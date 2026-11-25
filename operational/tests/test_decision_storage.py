"""Full-corpus database contract, exercised with deterministic scores in a disposable cluster."""

import hashlib
import json
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from time import monotonic, sleep
from uuid import uuid4
from zipfile import ZipFile

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from operational import worker
from operational.delivery_objects import encoded
from operational.delivery_objects import publish as publish_object
from operational.retention import capture_access, recover
from operational.tests.conftest import freeze_objects, generation, prepare_copy
from operational.tests.test_access import COOKIE, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_aspects import A, create
from operational.tests.test_deliveries import call, command, json_input
from operational.tests.test_retention import ask, fresh_restore_cluster, operator
from reviewscope.access.http import create_app
from reviewscope.access.ledger import read_witness
from reviewscope.access.operator import provision
from reviewscope.access.security import session_proof

PROFILE = "aspect-linear-lexical-v1"
FIELDS = {"id": "source_review_id", "text": "text"}
STANCES = ["complaint", "mixed", "denial", "neutral", "not_mentioned", "topic_uncertain"]


def query(conn, stmt, args):
    return conn.execute("SELECT " + stmt, args).fetchone()[0]


def runtime(w, proof):
    conn = psycopg.connect(user="rs_runtime", autocommit=True)
    conn.execute(
        "SELECT set_config('rs.workspace',%s,false),set_config('rs.session_proof',%s,false)",
        (str(w), proof),
    )
    return conn


@contextmanager
def cabinet(db, path, signing_key, *, size=None, allow=True, inputs=None):
    args, root, w, src = command(db, path)
    p = query(db, "project_id FROM source WHERE id=%s", (src,))
    if inputs is not None:
        rows = inputs
    elif size is None:
        accepted = [dict(id=str(i), text=f"Сцена {i}") for i in range(6)]
        accepted.extend([dict(id="blank", text=None), dict(id="blank-again", text=" ")])
        rows = [*accepted, accepted[0], False, 42, {"id": []}]
    else:
        rows = [dict(id=str(i), text=f"Сцена {i}") for i in range(size)]
    if size == 10000:
        from operational.tests.test_decision_resources import resource_input

        inp = resource_input(path, "repeated-10k")
        raw = path / "input.csv"
    else:
        inp = json_input(path, rows, FIELDS)
        raw = path / "data"
    snap = call([*args, *inp, "--apply"])["snapshot_id"]
    sha = hashlib.sha256(raw.read_bytes()).hexdigest()
    db.execute(
        """INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,decision_allowed)
           VALUES (%s,%s,%s,true,true,%s)""",
        (w, src, sha, allow),
    )
    pwd = secrets.token_urlsafe(24)
    provision(db, "annotator", pwd, signing_key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "annotator", pwd).status_code == 204
        saved = create(web, f"{ROOT}/workspaces/{w}/projects/{p}/aspects", A)
        proof = session_proof(web.cookies[COOKIE], signing_key)
        with runtime(w, proof) as reader:
            yield (
                (w, src, snap, saved["aspect_id"], saved["version_id"]),
                reader,
                proof,
                root,
                web,
                p,
            )


def enqueue(reader, scope, k=None):
    return reader.execute(
        "SELECT * FROM enqueue_decision(%s,%s,%s,%s,%s,%s,%s)", (*scope, k or uuid4(), PROFILE)
    ).fetchone()


def page(reader, scope, run, f=None, skip=0, version=None):
    return query(reader, "decision_read(%s,%s,%s,%s,%s,%s,%s)", (*scope[:3], run, f, skip, version))


def scores(db, scope):
    rows = db.execute(
        """SELECT v.id,v.content_sha256,v.text_content FROM snapshot_member m JOIN LATERAL (SELECT id,content_sha256,text_content FROM review_version
        WHERE (workspace_id,source_id,id)=(m.workspace_id,m.source_id,m.version_id) LIMIT 1) v ON true
        WHERE (m.workspace_id,m.source_id,m.snapshot_id)=(%s,%s,%s) ORDER BY v.id""",
        scope[:3],
    ).fetchall()
    results = []
    for i, (version, digest, text) in enumerate(rows):
        body = text or ""
        label = (
            STANCES[(int(body.split()[1]) if body.startswith("Сцена ") else i) % len(STANCES)]
            if body.strip()
            else "excluded_blank"
        )
        show = label not in {"excluded_blank", "not_mentioned"}
        results.append(
            dict(
                version_id=str(version),
                content_sha256=digest,
                text_sha256=hashlib.sha256(body.encode()).hexdigest(),
                outcome=label,
                start=0 if show else None,
                end=len(body) if show else None,
                score=None,
                reason="MODEL_CONTEXT"
                if show
                else ("BLANK" if label == "excluded_blank" else "ALL_WINDOWS_OFF_TOPIC"),
            )
        )
    return results


def stage(conn, claim, rows):
    return query(conn, "decision_stage(%s,%s,%s,%s)", (*claim, Jsonb(rows)))


def publish(conn, claim, n, root):
    doc = query(conn, "decision_manifest(%s,%s,%s)", claim)
    doc["runtime"] = dict(python="0", numpy="0", sklearn="0", dtype="test-double")
    raw = encoded(doc)
    sha = hashlib.sha256(raw).hexdigest()
    assert publish_object(root / str(claim[0]) / "sha256", (raw,)) == sha
    with conn.transaction():
        assert query(conn, "worker_step(%s,%s,%s,'publish',%s,%s)", (*claim, n, n)) == "running"
        conn.execute("SELECT set_config('rs.decision_projection',%s,true)", (raw.decode(),))
        assert (
            query(
                conn,
                "worker_step(%s,%s,%s,'publish',%s,%s,NULL,%s,%s,%s)",
                (*claim, n, n, sha, len(raw), doc["manifest_sha256"]),
            )
            == "succeeded"
        )
    return doc


def test_decision_accounting_fencing(db, tmp_path, key):  # noqa: F811
    with (
        cabinet(db, tmp_path, key, allow=False) as (s, reader, proof, root, web, _),
        worker.connect() as conn,
    ):
        assert enqueue(reader, s) == (404, None)
        db.execute("UPDATE browser_admission SET decision_allowed=true WHERE source_id=%s", (s[1],))
        k = uuid4()

        def same(_):
            with runtime(s[0], proof) as other:
                return enqueue(other, s, k)

        with ThreadPoolExecutor(2) as pool:
            a, b = pool.map(same, range(2))
        assert sorted([a[0], b[0]]) == [200, 202] and a[1] == b[1]
        run = a[1]
        assert enqueue(reader, (*s[:3], uuid4(), s[4]), k) == (404, None)
        assert enqueue(reader, (*s[:1], uuid4(), *s[2:]), k) == (404, None)
        rows = scores(db, s)
        assert len(rows) == 8
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        assert not query(conn, "decision_manifest(%s,%s,%s)", (*claim[:2], uuid4()))
        with pytest.raises(psycopg.DatabaseError, match="incomplete_decision_membership"):
            query(conn, "decision_manifest(%s,%s,%s)", claim)
        for bad in (
            dict(version_id=str(uuid4())),
            dict(content_sha256="f" * 64),
            dict(text_sha256="f" * 64),
            dict(start=0, end=500, outcome="complaint"),
        ):
            with pytest.raises(psycopg.DatabaseError):
                stage(conn, claim, [rows[0] | bad])
        with conn.transaction(force_rollback=True):
            assert stage(conn, claim, rows[:1]) == 1
        assert stage(conn, claim, rows[:1]) == 1
        with pytest.raises(psycopg.errors.UniqueViolation):
            stage(conn, claim, rows[:1])
        assert page(reader, s, run)["rows"] == []
        assert page(reader, s, run)["summary"] is None
        # A clean worker process starts from zero following process loss and lease expiry.
        db.execute(
            "UPDATE analysis_run SET heartbeat_at=now()-interval '30s',lease_until=now()-interval '2s' WHERE id=%s",
            (run,),
        )
        assert conn.execute("SELECT * FROM claim_job()").fetchone() is None
        assert query(db, "count(*) FROM decision_row WHERE run_id=%s", (run,)) == 0
        assert stage(conn, claim, rows) == 0
        db.execute("SELECT pg_sleep(1.05)")
        fresh = conn.execute("SELECT * FROM claim_job()").fetchone()
        assert fresh[1] == run and fresh[2] != claim[2] and fresh[3] == 2
        claim = fresh[:3]
        assert stage(conn, claim, rows) == 8
        for tamper in (dict(profile_sha256="d" * 64), dict(runtime={"dtype": "unversioned"})):
            with (
                pytest.raises(psycopg.DatabaseError, match="decision_manifest_mismatch"),
                conn.transaction(),
            ):
                wrong = query(conn, "decision_manifest(%s,%s,%s)", claim)
                wrong["runtime"] = dict(python="0", numpy="0", sklearn="0", dtype="test-double")
                raw = encoded(wrong | tamper)
                assert query(conn, "worker_step(%s,%s,%s,'publish',6,6)", claim) == "running"
                conn.execute("SELECT set_config('rs.decision_projection',%s,true)", (raw.decode(),))
                query(
                    conn,
                    "worker_step(%s,%s,%s,'publish',6,6,NULL,%s,%s,%s)",
                    (*claim, hashlib.sha256(raw).hexdigest(), len(raw), wrong["manifest_sha256"]),
                )
        assert page(reader, s, run)["summary"] is None
        doc = publish(conn, claim, 6, root)
        sums = doc["summary"]
        assert [
            sums[k]
            for k in (
                "N_input",
                "N_accepted",
                "N_quarantine",
                "N_confirmed_duplicate",
                "N_eligible",
                "N_evaluated",
                "N_mentioned",
                "N_complaints",
                "N_not_mentioned",
                "N_topic_uncertain",
            )
        ] == [12, 8, 3, 1, 6, 6, 4, 2, 1, 1]
        assert sums["coverage"] == 1 and sums["defined_coverage"] == 5 / 6
        assert (
            sums["complaints_among_eligible"] == 2 / 6
            and sums["complaints_among_mentions"] == 2 / 4
        )
        view = page(reader, s, run, "denial")
        assert view["filtered_total"] == 1 and view["rows"][0]["outcome"] == "denial"
        assert view["summary"] == sums
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute("DELETE FROM decision_row WHERE run_id=%s", (run,))
        for role in ("rs_runtime", "rs_worker", "rs_upload"):
            assert not query(
                db, "has_any_column_privilege(%s,'decision_row','SELECT,INSERT,UPDATE')", (role,)
            )
            assert not query(
                db,
                "has_any_column_privilege(%s,'decision_binding','SELECT,INSERT,UPDATE')",
                (role,),
            )
        assert page(reader, s, run, version=uuid4()) is None
        assert page(reader, (s[0], uuid4(), *s[2:]), run) is None
        assert page(reader, (uuid4(), *s[1:]), run) is None
        catalog = query(reader, "decision_catalog(%s,%s,%s,%s,%s)", s)
        assert catalog["runs"][0]["id"] == str(run)
        assert not any(k.startswith("session") for k in view)
        for reason in ("cancel", "grant", "session"):
            code, other = enqueue(reader, s)
            assert code == 202
            claimed = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
            assert stage(conn, claimed, rows[:1]) == 1
            if reason == "cancel":
                query(reader, "cancel_job(%s,%s)", (s[0], other))
            elif reason == "grant":
                db.execute(
                    "UPDATE browser_admission SET decision_allowed=false WHERE source_id=%s",
                    (s[1],),
                )
            else:
                db.execute("UPDATE access_session SET revoked_at=now() WHERE revoked_at IS NULL")
            assert stage(conn, claimed, rows[1:2]) == 0
            assert query(conn, "worker_step(%s,%s,%s,'summarize',0,6)", claimed) == (
                "cancelled" if reason == "cancel" else "failed"
            )
            assert query(db, "count(*) FROM decision_row WHERE run_id=%s", (other,)) == 0
            if reason != "cancel":
                assert page(reader, s, run) is None
            if reason == "grant":
                db.execute(
                    "UPDATE browser_admission SET decision_allowed=true WHERE source_id=%s", (s[1],)
                )


def test_decision_read_during_publication(db, tmp_path, key):  # noqa: F811
    with (
        cabinet(db, tmp_path, key) as (scope, reader, _, root, _, _),
        worker.connect() as conn,
        ThreadPoolExecutor(1) as pool,
    ):
        _, run = enqueue(reader, scope)
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        assert stage(conn, claim, scores(db, scope)) == 8
        with conn.transaction():
            # Fence holds the project lock while the reader enters its scoped read.
            assert query(conn, "decision_manifest(%s,%s,%s)", claim)
            waiting = pool.submit(page, reader, scope, run)
            until = monotonic() + 4
            while not query(
                db,
                " %s=ANY(pg_blocking_pids(%s))",
                (conn.info.backend_pid, reader.info.backend_pid),
            ):
                assert monotonic() < until
                sleep(0.02)
            publish(conn, claim, 6, root)
        first = waiting.result(timeout=5)
        assert first is not None and first["id"] == str(run)
        assert first["status"] == "running" and first["summary"] is None
        assert page(reader, scope, run)["summary"]["N_evaluated"] == 6


@pytest.mark.parametrize("kind", ["source", "project", "workspace"])
def test_decision_retention(db, tmp_path, monkeypatch, key, kind):  # noqa: F811
    with cabinet(db, tmp_path, key) as (s, reader, proof, root, web, p), worker.connect() as conn:
        w = s[0]
        rows = scores(db, s)
        _, run = enqueue(reader, s)
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        stage(conn, claim, rows)
        doc = publish(conn, claim, 6, root)
        _, unfinished = enqueue(reader, s)
        other = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        assert stage(conn, other, rows[:2]) == 2
        selected = {"source": s[1], "project": p, "workspace": w}[kind]
        req = ask(web, w, kind, selected, "export")
        out = tmp_path / "handoff"
        out.mkdir(mode=0o700)
        operator("export", "--request", req["id"], "--root", root, "--owner-handoff", out)
        with ZipFile(out / (req["id"] + ".zip")) as z:
            bindings = [
                json.loads(line) for line in z.read("rows/decision_binding.jsonl").splitlines()
            ]
            assert len(bindings) == 2 and all("session_binding" not in b for b in bindings)
            assert next(b for b in bindings if b["run_id"] == str(run))["manifest"] == doc
            assert next(b for b in bindings if b["run_id"] == str(unfinished))["manifest"] is None
            exported = [json.loads(line) for line in z.read("rows/decision_row.jsonl").splitlines()]
            assert len(exported) == 8 and all(r["run_id"] == str(run) for r in exported)
            assert all("text" not in r for r in exported)
        backup = tmp_path / "before.dump"
        subprocess.run(["pg_dump", "-Fc", "-f", backup], check=True)
        frozen = freeze_objects(root, tmp_path, [w])
        req = ask(web, w, kind, selected, "erase")
        assert (
            req["report"]["tables"]["decision_binding"] == 2
            and req["report"]["tables"]["decision_row"] == 10
        )
        operator(
            "confirm",
            "--request",
            req["id"],
            "--preview-sha256",
            req["preview_sha"],
            "--root",
            root,
        )
        operator("purge", "--request", req["id"], "--root", root)
        assert query(db, "count(*) FROM decision_row", ()) == 0
        proof, seal = read_witness()["sha256"], capture_access(db)
        with (
            fresh_restore_cluster(monkeypatch, backup) as (_, name),
            psycopg.connect(autocommit=True) as target,
        ):
            target.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
            )
            assert query(target, "count(*) FROM decision_row", ()) == 10
            token = prepare_copy(target, frozen, generation("decisions"), proof, seal)
            recover(target, proof, [token], seal)
            assert query(target, "count(*) FROM decision_row", ()) == 0
            assert query(target, "count(*) FROM decision_binding", ()) == 0
            assert query(target, "count(*) FROM aspect_version", ()) == int(kind == "source")


@pytest.mark.parametrize(
    ("texts", "accepted", "eligible", "label"),
    [
        ([dict(id="one", text="один"), dict(id="one", text="другой")], 0, 0, "excluded_blank"),
        ([dict(id="one", text="\u2003\t")], 1, 0, "excluded_blank"),
        ([dict(id="one", text="Сцена 0")], 1, 1, "not_mentioned"),
        ([dict(id="one", text="Сцена 0")], 1, 1, "topic_uncertain"),
    ],
)
def test_decision_empty_denominators(db, tmp_path, key, texts, accepted, eligible, label):  # noqa: F811
    with (
        cabinet(db, tmp_path, key, inputs=texts) as (s, reader, _, root, _, _),
        worker.connect() as conn,
    ):
        _, run = enqueue(reader, s)
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        assert query(conn, "job_input(%s,%s,%s)", claim)["decision"]["eligible"] == eligible
        rows = scores(db, s)
        for row in rows:
            row.update(
                outcome=label,
                start=None,
                end=None,
                reason="BLANK"
                if not eligible
                else ("LOW_MARGIN" if label == "topic_uncertain" else "ALL_WINDOWS_OFF_TOPIC"),
            )
        if rows:
            assert stage(conn, claim, rows) == accepted
        doc = publish(conn, claim, eligible, root)
        totals = page(reader, s, run)["summary"]
        assert (
            totals == doc["summary"]
            and totals["N_accepted"] == accepted
            and totals["N_eligible"] == eligible
        )
        assert totals["coverage"] == (1 if eligible else None)
        assert totals["complaints_among_eligible"] == (0 if eligible else None)
        assert (
            totals["complaints_among_mentions"] is None
            and totals["undefined_rates"]["complaints_among_mentions"] == "NO_MENTIONS"
        )
        if label == "topic_uncertain":
            assert totals["defined_coverage"] == 0 and totals["N_uncertain"] == 1


def eligibility_plan(claim, mode, analyzed):
    scans = []

    def observe(notice):
        _, marker, raw = notice.message_primary.partition("plan:\n")
        if marker and "missing_text'=ANY(ir.warnings)" in raw:
            plan = json.loads(raw)["Plan"]
            pending = [plan]
            while pending:
                node = pending.pop()
                pending.extend(node.get("Plans", []))
                if node.get("Relation Name") == "import_row":
                    scans.append(
                        dict(
                            index=node.get("Index Name"),
                            rows=node["Actual Rows"],
                            loops=node["Actual Loops"],
                            blocks=node["Shared Hit Blocks"] + node["Shared Read Blocks"],
                        )
                    )

    with worker.connect() as measured:
        measured.execute("SELECT set_config('plan_cache_mode',%s,false)", (mode,))
        measured.add_notice_handler(observe)
        before = monotonic()
        data = query(measured, "job_input(%s,%s,%s)", claim)
        elapsed = monotonic() - before
    assert data["decision"]["eligible"] == 10000 and scans
    assert all(node["rows"] == 10000 and node["loops"] == 1 for node in scans), scans
    return dict(mode=mode, analyzed=analyzed, seconds=round(elapsed, 4), scans=scans)


def test_decision_10k_membership_and_pages(db, tmp_path, key):  # noqa: F811
    from operational.tests.reviewer_plan_probe import enable_client_plans

    for table in ("decision_binding", "decision_row", "review_version", "snapshot_member"):
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    with (
        cabinet(db, tmp_path, key, size=10000) as (s, reader, proof, root, web, p),
        worker.connect() as conn,
    ):
        _, run = enqueue(reader, s)
        expected = scores(db, s)
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        start_input = monotonic()
        assert query(conn, "job_input(%s,%s,%s)", claim)["decision"]["eligible"] == len(expected)
        input_seconds = monotonic() - start_input
        assert len(expected) == 10000
        enable_client_plans(db, "rs_worker")
        input_plans = []
        for analyzed in (False, True):
            if analyzed:
                db.execute("ANALYZE review_version,snapshot_member,import_row")
            for mode in ("force_generic_plan", "force_custom_plan"):
                assert query(conn, "worker_step(%s,%s,%s,'summarize',0,10000)", claim) == "running"
                input_plans.append(eligibility_plan(claim, mode, analyzed))
        started = monotonic()
        for i in range(0, len(expected), 64):
            assert (
                query(conn, "worker_step(%s,%s,%s,'summarize',%s,10000)", (*claim, i)) == "running"
            )
            assert stage(conn, claim, expected[i : i + 64]) == len(expected[i : i + 64])
        staged = monotonic()
        doc = publish(conn, claim, 10000, root)
        ended = monotonic()
        bytes_used = query(db, "sum(pg_column_size(d)) FROM decision_row d WHERE run_id=%s", (run,))
        assert doc["summary"]["N_evaluated"] == 10000
        seen = []
        max_bytes = 0
        for i in range(0, 10000, 25):
            reply = page(reader, s, run, skip=i)
            assert reply["filtered_total"] == 10000 and len(reply["rows"]) == 25
            seen.extend(r["version_id"] for r in reply["rows"])
            max_bytes = max(max_bytes, len(encoded(reply)))
        assert seen == [r["version_id"] for r in expected] and max_bytes <= 65536
        enable_client_plans(db)
        observations = []
        for analyzed in (False, True):
            if analyzed:
                db.execute("ANALYZE decision_row,decision_binding,review_version,snapshot_member")
            for mode in ("force_generic_plan", "force_custom_plan"):
                with runtime(s[0], proof) as measured:
                    measured.execute("SELECT set_config('plan_cache_mode',%s,false)", (mode,))
                    plans = []

                    def observe(notice, plans=plans):
                        _, marker, raw = notice.message_primary.partition("plan:\n")
                        if marker and "WITH hits AS MATERIALIZED" in raw:
                            plan = json.loads(raw)["Plan"]
                            pending = [plan]
                            inspected = 0
                            indexes = []
                            while pending:
                                node = pending.pop()
                                pending.extend(node.get("Plans", []))
                                if (
                                    node.get("Relation Name") == "decision_row"
                                    and node["Actual Loops"]
                                ):
                                    inspected += node["Actual Loops"] * (
                                        node["Actual Rows"] + node.get("Rows Removed by Filter", 0)
                                    )
                                    indexes.append(node.get("Index Name", node["Node Type"]))
                            plans.append(
                                dict(
                                    rows=inspected,
                                    indexes=indexes,
                                    blocks=plan["Shared Hit Blocks"] + plan["Shared Read Blocks"],
                                )
                            )

                    measured.add_notice_handler(observe)
                    for label, offset, detail in (
                        (None, 0, None),
                        (None, 9975, None),
                        ("denial", 25, None),
                        (None, 0, seen[-1]),
                    ):
                        before = monotonic()
                        view = page(measured, s, run, label, offset, detail)
                        assert view["rows"]
                        observations.append(
                            dict(
                                analyzed=analyzed,
                                mode=mode,
                                filter=label,
                                offset=offset,
                                detail=bool(detail),
                                ms=round((monotonic() - before) * 1000, 2),
                                **plans[-1],
                            )
                        )
                    assert plans[-1]["rows"] <= 1
        print(
            json.dumps(
                dict(
                    probe="decision-storage-10k",
                    profile="deterministic_test_scorer",
                    rows=len(seen),
                    pages=400,
                    stage_s=round(staged - started, 3),
                    input_s=round(input_seconds, 3),
                    publish_s=round(ended - staged, 3),
                    stage_row_bytes=bytes_used,
                    manifest_bytes=len(encoded(doc)),
                    max_response_bytes=max_bytes,
                    plans=observations,
                    input_plans=input_plans,
                )
            )
        )
