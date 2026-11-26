"""Measure the actual admitted snapshot path, including explicit pre-computation refusals."""

import json
import os
import secrets
from hashlib import sha256
from pathlib import Path
from time import monotonic
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational.tests.reviewer_plan_probe import enable_client_plans
from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_decision_resources import resource_input
from operational.tests.test_decision_storage import cabinet, runtime
from operational.tests.test_deliveries import call, command
from operational.tests.test_human_review import operation, submit
from operational.worker import linux_peak_kib
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision

SIZES = {
    "a": (3, 3),
    "b": (6, 6),
    "problematic": (12, 6),
    "public-demo": (1058, 1058),
    "diverse-4mib-10k": (10000, 10000),
    "repeated-10k": (10000, 10000),
}


@pytest.mark.parametrize(
    "case", list(SIZES) if os.environ.get("RS_MEASURE_TOPICS") == "1" else list(SIZES)[:3]
)
def test_topic_discovery_capacity(db, tmp_path, key, monkeypatch, case):  # noqa: F811
    args, _, w, src = command(db, tmp_path)
    flags = resource_input(tmp_path, case)
    file = Path(flags[flags.index("--input") + 1])
    snapshot = call(args + flags + ["--apply"])["snapshot_id"]
    if "10k" in case:
        assert file.stat().st_size == 4194304
    digest = sha256(file.read_bytes()).hexdigest()
    db.execute(
        "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,topic_discovery_allowed) VALUES (%s,%s,%s,true,true,true)",
        (w, src, digest),
    )
    pw = secrets.token_urlsafe(24)
    provision(db, "capacity-analyst", pw, key, workspace=w, role="analyst")
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "capacity-analyst", pw).status_code == 204
        route = f"{ROOT}/workspaces/{w}/snapshots/{snapshot}/topic-discovery"
        q = {"source_id": str(src)}
        cap = web.get(route, params=q).json()
        assert (cap["N_input"], cap["N_accepted"]) == SIZES[case]
        tick = monotonic()
        reply = web.post(route, headers=CSRF, json=operation(src))
        report = dict(
            case=case,
            N_input=cap["N_input"],
            N_accepted=cap["N_accepted"],
            input_bytes=file.stat().st_size,
            publish_or_refusal_s=monotonic() - tick,
            response_bytes=len(reply.content),
        )
        if cap["N_accepted"] > 512:
            assert cap["reason"] == "VERSION_LIMIT" and reply.status_code == 413
            assert reply.json() == {
                "error": "RESOURCE_LIMIT",
                "reason": "VERSION_LIMIT",
                "processed": 0,
            }
            assert db.execute("SELECT count(*) FROM topic_result").fetchone() == (0,)
            report.update(reply.json())
            # An operator-only stress run of exactly the same producer on canonical versions.
            backend = db.info.backend_pid

            def cpu_seconds():
                stat = Path(f"/proc/{backend}/stat").read_text().rsplit(")", 1)[1].split()
                return (int(stat[11]) + int(stat[12])) / os.sysconf("SC_CLK_TCK")

            wall, cpu = monotonic(), cpu_seconds()
            try:
                with db.transaction():
                    db.execute("SET LOCAL statement_timeout='10s'")
                    db.execute("SET LOCAL work_mem='8MB'")
                    counts = db.execute(
                        """SELECT count(*),count(group_sha),count(DISTINCT group_sha),count(term),count(DISTINCT term)
                        FROM topic_projection(%s,%s,%s,NULL,NULL)""",
                        (w, src, snapshot),
                    ).fetchone()
                    assert counts[0] == cap["N_accepted"]
                    report["offline_projection"] = dict(
                        status="COMPLETE",
                        processed=counts[0],
                        eligible=counts[1],
                        unique=counts[2],
                        clustered=counts[3],
                        noise=counts[1] - counts[3],
                        candidates=counts[4],
                    )
            except psycopg.errors.QueryCanceled:
                report["offline_projection"] = dict(
                    status="RESOURCE_LIMIT", stage="projection", sql_limit_s=10, committed=0
                )
            report["offline_projection"].update(
                wall_s=monotonic() - wall,
                cpu_s=cpu_seconds() - cpu,
                pg_backend_hwm_kib=linux_peak_kib(backend),
            )

        else:
            reply.raise_for_status()
            m = web.get(route + "/" + reply.json()["id"], params=q).json()["manifest"]
            report.update({k: v for k, v in m.items() if k.startswith("N_")})
            assert m["N_selected"] == m["N_clustered"] + m["N_noise"] and m["N_not_processed"] == 0
            if case == "problematic":
                assert (
                    m["N_quarantine"],
                    m["N_confirmed_duplicate"],
                    m["N_blank"],
                    m["status"],
                ) == (5, 1, 6, "NO_ELIGIBLE_TEXTS")
        report["parent_hwm_kib"] = linux_peak_kib("self")
        report["external_staging_bytes"] = 0  # No worker or staged artifact in this profile.
        print("TOPIC_CAPACITY", json.dumps(report, sort_keys=True))


def test_topic_discovery_bounded_plans(db, tmp_path, key, monkeypatch):  # noqa: F811
    # Exercise many candidates and a separate large group of exact copies.
    docs = [
        {"id": str(i), "text": f"Термин{i // 4:03d} рубрика{i // 4:03d} деталь{i:03d}"}
        for i in range(256)
    ]
    docs += [{"id": "copy" + str(i), "text": docs[0]["text"]} for i in range(256)]
    for table in ["topic_input", "topic_candidate", "topic_result", "topic_event"]:
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    with (
        cabinet(db, tmp_path, key, inputs=docs) as (scope, _, proof, _, web, _),
        importer(tmp_path, monkeypatch),
    ):
        w, src, s = scope[:3]
        db.execute(
            "UPDATE browser_admission SET topic_discovery_allowed=true WHERE source_id=%s", (src,)
        )
        route = f"{ROOT}/workspaces/{w}/snapshots/{s}/topic-discovery"
        tick = monotonic()
        res = submit(web, route, operation(src))["id"]
        publish_s = monotonic() - tick
        q = {"source_id": str(src)}
        page = web.get(route + "/" + res, params=q)
        m = page.json()["manifest"]
        assert [m[k] for k in ("N_selected", "N_unique_inputs", "N_noise", "candidates")] == [
            512,
            256,
            0,
            64,
        ]
        candidate = db.execute(
            "SELECT id FROM topic_candidate ORDER BY cardinality(members) DESC LIMIT 1"
        ).fetchone()[0]
        assert db.execute("SELECT topic_intact(%s,%s,%s)", (w, s, res)).fetchone() == (True,)
        reads = []
        for off in (0, 0, 250):
            tick = monotonic()
            detail = web.get(
                route + "/" + res, params=q | {"candidate_id": str(candidate), "offset": off}
            )
            detail.raise_for_status()
            reads.append(dict(offset=off, seconds=monotonic() - tick, bytes=len(detail.content)))
            assert len(detail.json()["rows"]) == min(25, 260 - off) and len(detail.content) <= 65536
        enable_client_plans(db)
        observations = []
        for analyzed in (False, True):
            if analyzed:
                for tab in [
                    "topic_input",
                    "topic_result",
                    "topic_candidate",
                    "topic_event",
                    "snapshot_member",
                    "review_version",
                ]:
                    db.execute("ANALYZE " + tab)
            for mode in ("force_generic_plan", "force_custom_plan"):
                scans = {}

                def summarize(notice, scans=scans):
                    _, sep, raw = notice.message_primary.partition("plan:\n")
                    if not sep:
                        return
                    stack = [json.loads(raw)["Plan"]]
                    while stack:
                        node = stack.pop()
                        stack.extend(node.get("Plans", []))
                        table = node.get("Relation Name")
                        if table not in (
                            "topic_input",
                            "topic_event",
                            "topic_candidate",
                            "snapshot_member",
                            "review_version",
                        ):
                            continue
                        visited = node["Actual Loops"] * (
                            node["Actual Rows"] + node.get("Rows Removed by Filter", 0)
                        )
                        old = scans.get(table, (0, 0, 0))
                        scans[table] = tuple(
                            max(a, b)
                            for a, b in zip(
                                old,
                                (visited, node["Actual Loops"], node.get("Shared Hit Blocks", 0)),
                                strict=True,
                            )
                        )

                with runtime(w, proof) as reader:
                    reader.execute("SET statement_timeout='10s'")
                    reader.execute("SELECT set_config('plan_cache_mode',%s,false)", (mode,))
                    reader.add_notice_handler(summarize)
                    reader.execute("BEGIN")
                    assert (
                        reader.execute(
                            "SELECT topic_catalog(%s,%s,%s,NULL,NULL)", (w, src, s)
                        ).fetchone()[0]["N_accepted"]
                        == 512
                    )
                    assert (
                        reader.execute(
                            "SELECT topic_read(%s,%s,%s,%s,%s,NULL,250)",
                            (w, src, s, res, candidate),
                        ).fetchone()[0]["total"]
                        == 260
                    )
                    assert (
                        reader.execute(
                            "SELECT topic_review(%s,%s,%s,%s,%s,'reject',%s,'Проверено',NULL,NULL,NULL,NULL)",
                            (w, src, s, res, uuid4(), [candidate]),
                        ).fetchone()[0]["code"]
                        == 201
                    )
                    peak = linux_peak_kib(reader.info.backend_pid)
                    reader.execute("ROLLBACK")
                assert scans and max(v[0] for v in scans.values()) <= 1024, scans
                observations.append(dict(analyzed=analyzed, mode=mode, max_rows_loops_hits=scans))
        long_parts = [[str(uuid4())], [str(uuid4())]]
        submit(
            web,
            route + "/" + res + "/actions",
            operation(
                src,
                action="split",
                expected_open=[str(candidate)],
                parts=long_parts,
                reason="Размер",
            ),
            422,
        )
        print(
            "TOPIC_STORAGE",
            json.dumps(
                dict(
                    publish_s=publish_s,
                    manifest_bytes=len(json.dumps(m).encode()),
                    reads=reads,
                    pg_backend_hwm_kib=peak,
                    parent_hwm_kib=linux_peak_kib("self"),
                    plans=observations,
                )
            ),
        )
