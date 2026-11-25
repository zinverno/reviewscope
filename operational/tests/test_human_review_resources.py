"""Measure human operations on assigned machine labels in an isolated PostgreSQL cluster."""

import hashlib
import json
import resource
from pathlib import Path
from time import monotonic
from uuid import UUID, uuid4
from zipfile import ZipFile

import psycopg
from psycopg.types.json import Jsonb

from operational import worker
from operational.tests.reviewer_plan_probe import enable_client_plans
from operational.tests.test_access import CSRF, ROOT, key  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_decision_storage import (
    cabinet,
    enqueue,
    publish,
    query,
    runtime,
    scores,
    stage,
)
from operational.tests.test_retention import ask, operator


def queue(conn, scope, mode="all", label=None, state=None, plan=None, offset=0):
    return query(
        conn, "human_queue(%s,%s,%s,%s,%s,%s,%s,%s,%s)", (*scope, mode, label, state, plan, offset)
    )


def plan_profiles(w, proof, scope, sample_id, expected_sample, analyzed):
    observations = []
    for mode in ("force_generic_plan", "force_custom_plan"):
        with runtime(w, proof) as measured:
            measured.execute("SELECT set_config('plan_cache_mode',%s,false)", (mode,))
            nested = []

            def observe(notice, nested=nested):
                _, marker, raw = notice.message_primary.partition("plan:\n")
                if not marker or "WITH heads AS (" not in raw:
                    return
                plan = json.loads(raw)["Plan"]
                nodes = [plan]
                scans = []
                while nodes:
                    node = nodes.pop()
                    nodes.extend(node.get("Plans", []))
                    if node.get("Relation Name") in {
                        "human_review_event",
                        "decision_row",
                        "human_plan_member",
                    }:
                        per_loop = (
                            node["Actual Rows"]
                            + node.get("Rows Removed by Filter", 0)
                            + node.get("Rows Removed by Index Recheck", 0)
                        )
                        scans.append(per_loop * node["Actual Loops"])
                nested.append((max(scans), plan["Shared Hit Blocks"] + plan["Shared Read Blocks"]))

            measured.add_notice_handler(observe)
            for kind, label, state, offset in (
                ("all", None, None, 0),
                ("all", None, None, 0),
                ("all", None, None, 9975),
                ("all", "not_mentioned", None, 25),
                ("all", None, "confirmed", 0),
                ("uncertain", None, None, 0),
                ("disagreement", None, None, 0),
                ("sample", None, None, 0),
                ("sample", None, None, 75),
                ("sample", None, None, 9975),
            ):
                tick = monotonic()
                page = queue(
                    measured,
                    scope,
                    kind,
                    label,
                    state,
                    sample_id if kind == "sample" else None,
                    offset,
                )
                elapsed = monotonic() - tick
                assert page["eligible"] == 10000 and 0 < len(page["rows"]) <= 25
                if kind == "sample":
                    assert [row["version_id"] for row in page["rows"]] == expected_sample[
                        offset : offset + 25
                    ]
                if kind == "disagreement":
                    assert all(row["state"] == "disputed" for row in page["rows"])
                visits, blocks = nested.pop()
                assert visits <= 20000 and elapsed < 12
                observations.append(
                    dict(
                        analyzed=analyzed,
                        mode=mode,
                        queue=kind,
                        label=label,
                        state=state,
                        offset=offset,
                        ms=round(elapsed * 1000, 2),
                        max_scan_rows=visits,
                        shared_blocks=blocks,
                    )
                )
    return observations


def publication_plan(db, scope, revision):
    # Measure the exact SQL function separately from the production transaction and its FK checks.
    enable_client_plans(db, "rs_migration")
    plans = []
    with psycopg.connect(user="rs_migration", autocommit=True) as explained:

        def observe(notice):
            _, marker, raw = notice.message_primary.partition("plan:\n")
            if not marker or "WITH baseline AS (" not in raw or "human-resolved-v1" not in raw:
                return
            root = json.loads(raw)["Plan"]
            nodes = [root]
            visits = []
            while nodes:
                node = nodes.pop()
                nodes.extend(node.get("Plans", []))
                rows = node["Actual Rows"] + sum(
                    node.get("Rows Removed by " + key, 0)
                    for key in ("Filter", "Join Filter", "Index Recheck")
                )
                visits.append(
                    (rows * node["Actual Loops"], node["Node Type"], node.get("Relation Name"))
                )
            cost, operation, table = max(visits, key=lambda item: item[0])
            plans.append(
                dict(
                    max_visits=cost,
                    operation=operation,
                    relation=table,
                    shared_blocks=root["Shared Read Blocks"] + root["Shared Hit Blocks"],
                )
            )

        explained.add_notice_handler(observe)
        assert query(explained, "human_result_document(%s,%s,%s)", (scope[0], scope[3], revision))
    assert len(plans) == 1
    return plans[0]


def test_human_review_storage_10k(db, tmp_path, monkeypatch, key):  # noqa: F811
    tables = (
        "human_review_event",
        "human_review_plan",
        "human_plan_member",
        "human_review_result",
        "human_result_member",
        "decision_row",
        "decision_binding",
        "review_version",
        "snapshot_member",
    )
    for table in tables:
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    with (
        cabinet(db, tmp_path, key, size=10000) as (s, reader, proof, root, web, _),
        worker.connect() as conn,
        importer(tmp_path, monkeypatch),
    ):
        w, src, snap = s[:3]
        assert (tmp_path / "input.csv").stat().st_size == 4 * 1024 * 1024
        _, run = enqueue(reader, s)
        scope = (w, src, snap, run)
        assigned = scores(db, s)
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        assert len(assigned) == 10000
        # Insert in descending order. The independent sampling oracle iterates in ascending order.
        reverse = assigned[::-1]
        for start in range(0, len(reverse), 64):
            assert (
                query(conn, "worker_step(%s,%s,%s,'summarize',%s,10000)", (*claim, start))
                == "running"
            )
            assert stage(conn, claim, reverse[start : start + 64]) == len(
                reverse[start : start + 64]
            )
        doc = publish(conn, claim, 10000, root)
        db.execute(
            "UPDATE browser_admission SET human_review_allowed=true WHERE source_id=%s", (src,)
        )
        seed = uuid4()
        prefix = b"human-sample-v1" + w.bytes + run.bytes + seed.bytes
        expected_sample = sorted(
            (row["version_id"] for row in assigned),
            key=lambda version: (hashlib.sha256(prefix + UUID(version).bytes).digest(), version),
        )
        tick = monotonic()
        plan = query(reader, "human_plan(%s,%s,%s,%s,%s,%s,10000)", (*scope, uuid4(), seed))
        plan_ms = round((monotonic() - tick) * 1000, 2)
        assert plan["code"] == 201
        first = []
        for offset in range(0, 100, 25):
            first.extend(queue(reader, scope, "sample", plan=plan["id"], offset=offset)["rows"])
        assert [row["version_id"] for row in first] == expected_sample[:100]
        assert any(row["outcome"] == "not_mentioned" for row in assigned)
        print("10k synthetic plan created", flush=True)

        append = "SELECT human_append(%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,'all',NULL)"
        ticks = {}
        first_append_ms = None
        event_ids = {}
        versions = []
        receipts = []
        began = monotonic()
        for start in range(0, 10000, 50):
            with reader.transaction():
                # Bounded batches retain all checks of the normal runtime writer on every row.
                for i, row in enumerate(assigned[start : start + 50], start):
                    machine = row["outcome"]
                    if i % 50 == 0:
                        action, label = "dispute", None
                    elif i % 50 == 1:
                        action, label = (
                            "correct",
                            "denial" if machine == "complaint" else "complaint",
                        )
                    else:
                        action, label = "confirm", machine
                    spans = [{"start": 0, "end": 1}] if label in {"complaint", "mixed"} else []
                    if i == 0:
                        first_tick = monotonic()
                    response = reader.execute(
                        append,
                        (
                            *scope,
                            row["version_id"],
                            uuid4(),
                            action,
                            label,
                            "Синтетическая проверка",
                            Jsonb(spans),
                        ),
                    ).fetchone()[0]
                    if i == 0:
                        first_append_ms = round((monotonic() - first_tick) * 1000, 2)
                    assert response["code"] == 201
                    event_ids[row["version_id"]] = response["id"]
            size = start + 50
            if size in (100, 1000, 10000):
                ticks[size] = round(monotonic() - began, 3)
                print(f"Synthetic {size} events in {ticks[size]}s", flush=True)
            if size in (1000, 10000):
                tick = monotonic()
                with reader.transaction():
                    reply = query(reader, "human_publish(%s,%s,%s,%s,%s)", (*scope, uuid4()))
                    assert reply["code"] == 201
                    sql_done = monotonic()
                elapsed = monotonic() - tick
                versions.append(reply["id"])
                receipts.append(round(elapsed * 1000, 2))
                print(
                    json.dumps(
                        dict(
                            probe="human-publication",
                            members=size,
                            sql_ms=round((sql_done - tick) * 1000, 2),
                            commit_ms=round((elapsed - (sql_done - tick)) * 1000, 2),
                        )
                    ),
                    flush=True,
                )
        publication = publication_plan(db, scope, versions[-1])
        print(json.dumps(dict(probe="human-publication-plan", **publication)), flush=True)
        assert publication["max_visits"] <= 2 * len(event_ids)
        assert max(receipts) < 12000
        results_url = f"{ROOT}/workspaces/{w}/snapshots/{snap}/decisions/{run}/human/results"
        body = {"source_id": str(src), "idempotency_key": str(uuid4())}
        tick = monotonic()
        published = web.post(results_url, headers=CSRF, json=body)
        http_publish_ms = round((monotonic() - tick) * 1000, 2)
        assert published.status_code == 201 and http_publish_ms < 12000
        http_revision = published.json()["id"]
        assert http_revision not in versions
        page = web.get(f"{results_url}/{http_revision}", params={"source_id": str(src)})
        assert page.status_code == 200 and page.json()["summary"]["reviewed"] == 10000
        original = query(reader, "human_result(%s,%s,%s,%s,%s,0)", (*scope, versions[-1]))
        assert page.json()["digest"] == original["digest"]
        for index, revision in enumerate(versions):
            count = (1000, 10000)[index]
            found = {}
            for offset in range(0, 10000, 25):
                page = query(reader, "human_result(%s,%s,%s,%s,%s,%s)", (*scope, revision, offset))
                assert page["total"] == 10000 and page["summary"]["reviewed"] == count
                assert page["summary"]["disputed"] == count // 50
                assert page["rows_sha256"] == doc["rows_sha256"]
                for row in page["rows"]:
                    if row["event_id"]:
                        found[row["version_id"]] = row["event_id"]
            assert found == dict(list(event_ids.items())[:count])
            print(f"Checked frozen IDs for {count} human events", flush=True)
        assert query(db, "human_integrity(%s,%s)", (w, run))
        enable_client_plans(db)
        plans = plan_profiles(w, proof, scope, plan["id"], expected_sample, False)
        db.execute("ANALYZE " + ",".join(tables))
        plans += plan_profiles(w, proof, scope, plan["id"], expected_sample, True)
        second = query(reader, "human_plan(%s,%s,%s,%s,%s,%s,10000)", (*scope, uuid4(), seed))
        by_plan = query(reader, "human_plans(%s,%s,%s,%s,0)", scope)["items"]
        assert len(by_plan) == 2 and by_plan[0]["digest"] == by_plan[1]["digest"]
        assert second["id"] != plan["id"] and query(db, "human_integrity(%s,%s)", (w, run))

        url = f"{ROOT}/workspaces/{w}/snapshots/{snap}/decisions/{run}/human/queue"
        api_pages = []
        for offset in (0, 0, 9975):
            t = monotonic()
            res = web.get(url, params={"source_id": str(src), "offset": offset})
            assert res.status_code == 200, res.text
            assert len(res.content) <= 65536 and monotonic() - t < 12
            api_pages.append(dict(ms=round((monotonic() - t) * 1000, 2), bytes=len(res.content)))
        relation_bytes = {
            table: query(db, "pg_total_relation_size(%s::regclass)", (table,))
            for table in tables[:5]
        }
        req = ask(web, w, "source", src, "export")
        out = tmp_path / "handoff"
        out.mkdir(mode=0o700)
        t = monotonic()
        operator("export", "--request", req["id"], "--root", root, "--owner-handoff", out)
        export_s = round(monotonic() - t, 3)
        path = out / (req["id"] + ".zip")
        with ZipFile(path) as archive:
            with archive.open("rows/human_review_event.jsonl") as stream:
                assert {json.loads(line)["id"] for line in stream} == set(event_ids.values())
        request = ask(web, w, "source", src, "erase")
        t = monotonic()
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
        purge_s = round(monotonic() - t, 3)
        for table in tables[:5]:
            assert (
                query(db, f"count(*) FROM {table} WHERE workspace_id=%s AND run_id=%s", (w, run))
                == 0
            )
        print(
            json.dumps(
                dict(
                    probe="human-review-storage-10k",
                    corpus=10000,
                    input_bytes=4194304,
                    plan_ms=plan_ms,
                    first_append_ms=first_append_ms,
                    relation_bytes=relation_bytes,
                    append_seconds=ticks,
                    publish_ms=receipts,
                    publication_plan=publication,
                    http_publish_ms=http_publish_ms,
                    checked_memberships=11000,
                    versions=versions,
                    plans=plans,
                    api=api_pages,
                    export_s=export_s,
                    export_bytes=path.stat().st_size,
                    purge_s=purge_s,
                    test_process_peak_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                    backend_peak_kib=next(
                        int(line.split()[1])
                        for line in Path(f"/proc/{reader.info.backend_pid}/status")
                        .read_text()
                        .splitlines()
                        if line.startswith("VmHWM:")
                    ),
                )
            )
        )
