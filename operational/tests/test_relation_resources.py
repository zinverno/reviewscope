"""Import actual source bytes, account for every position, measure reads and lexical workload."""

import hashlib
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_decision_resources import resource_input
from operational.tests.test_deliveries import call, command, inputs
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision

CASES = {"a": (3, 3), "b": (6, 6), "problematic": (12, 6)}
LARGE = {"public-demo": (1058, 1058), "diverse-10k": (10000, 10000), "equal-10k": (10000, 10000)}


@pytest.mark.parametrize(
    "name", list(CASES) + (list(LARGE) if os.getenv("RS_MEASURE_RELATIONS") == "1" else [])
)
def test_relation_snapshot_profile(db, tmp_path, key, monkeypatch, name):  # noqa: F811
    args, _, w, src = command(db, tmp_path)
    if name == "equal-10k":
        path = tmp_path / "input.csv"
        path.write_text("id,body\n" + "".join(f"{i},Один и тот же талон\n" for i in range(10000)))
        (tmp_path / "mapping").write_text(
            '{"version":"resource-1","fields":{"id":"source_review_id","body":"text"}}'
        )
        (tmp_path / "passport").write_text(
            '{"delimiter":",","source":"synthetic","source_place_id":"p1"}'
        )
        payload = inputs(path, tmp_path / "mapping", tmp_path / "passport", "csv")
    else:
        payload = resource_input(tmp_path, name)
        path = Path(payload[payload.index("--input") + 1])
    data = call(args + payload + ["--apply"])
    assert (data["input"], data["accepted"]) == (CASES | LARGE)[name]
    if name == "problematic":
        assert data["quarantined"] == 5 and data["confirmed_duplicates"] == 1
    snap = data["snapshot_id"]
    db.execute(
        "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,relation_allowed) VALUES (%s,%s,%s,true,true,true)",
        (w, src, hashlib.sha256(path.read_bytes()).hexdigest()),
    )
    secret = secrets.token_urlsafe(24)
    provision(db, "relation-probe", secret, key, workspace=w, role="analyst")
    url = f"{ROOT}/workspaces/{w}/snapshots/{snap}/relations"
    report = dict(
        probe="relation-profile",
        fixture=name,
        positions=data["input"],
        accepted=data["accepted"],
        input_bytes=path.stat().st_size,
    )
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "relation-probe", secret).status_code == 204
        q = dict(source_id=str(src))
        tick = monotonic()
        response = web.post(url, headers=CSRF, json=q | dict(idempotency_key=str(uuid4())))
        report["publish_s"] = monotonic() - tick
        report["http"] = response.status_code
        if name in LARGE:
            assert response.status_code == 413
            assert web.get(url, params=q).json()["reason"] == "VERSION_LIMIT"
            assert db.execute(
                "SELECT count(*) FROM relation_result WHERE workspace_id=%s", (w,)
            ).fetchone() == (0,)
        else:
            assert response.status_code == 201, response.text
            page_url = url + "/" + response.json()["id"]
            for phase in ("first", "repeat", "last"):
                tick = monotonic()
                page = web.get(
                    page_url,
                    params=q
                    | {
                        "offset": max(0, ((report.get("pairs", 0) - 1) // 10) * 10)
                        if phase == "last"
                        else 0
                    },
                )
                page.raise_for_status()
                report[phase + "_s"] = monotonic() - tick
                report[phase + "_bytes"] = len(page.content)
                report["pairs"] = page.json()["manifest"]["candidate_pairs_count"]
                assert len(page.content) <= 65536 and len(page.json()["pairs"]) <= 10
    rows = db.execute(
        """SELECT m.version_id::text,v.text_content FROM snapshot_member m JOIN review_version v
    ON (v.workspace_id,v.source_id,v.id)=(m.workspace_id,m.source_id,m.version_id)
    WHERE (m.workspace_id,m.source_id,m.snapshot_id)=(%s,%s,%s) ORDER BY m.version_id""",
        (w, src, snap),
    ).fetchall()
    run = subprocess.run(
        [sys.executable, "-m", "scripts.profile_relations"],
        input=json.dumps(rows, ensure_ascii=False).encode(),
        capture_output=True,
        check=True,
        timeout=120,
    )
    report["lexical"] = json.loads(run.stdout)
    if name == "equal-10k":
        assert (
            report["lexical"][0]["exact_possible_pairs"] == 49_995_000
            and report["lexical"][0]["status"] == "PAIR_BUDGET_LIMIT"
        )
    else:
        assert report["lexical"][0]["status"] == "complete"
    print(json.dumps(report), flush=True)


def test_typed_relations_storage(db, tmp_path, key, monkeypatch):  # noqa: F811
    from zipfile import ZipFile

    from operational.tests.reviewer_plan_probe import enable_client_plans
    from operational.tests.test_decision_storage import cabinet, runtime
    from operational.tests.test_retention import ask, operator
    from operational.worker import linux_peak_kib

    docs = [dict(id=str(i), text="Одна готовая корзина на стойке.") for i in range(32)]
    for table in ("relation_result", "relation_pair"):
        db.execute(f"ALTER TABLE {table} SET (autovacuum_enabled=false)")
    with (
        cabinet(db, tmp_path, key, inputs=docs) as (scope, reader, proof, root, web, _),
        importer(tmp_path, monkeypatch),
    ):
        w, src, s, *_ = scope
        db.execute("UPDATE browser_admission SET relation_allowed=true WHERE source_id=%s", (src,))
        enable_client_plans(db)
        reader.execute("SET statement_timeout='10s'")
        tick = monotonic()
        for _ in range(21):
            saved = reader.execute(
                "SELECT relation_publish(%s,%s,%s,NULL,NULL,%s)", (w, src, s, uuid4())
            ).fetchone()[0]
            assert saved["code"] == 201
        res = saved["id"]
        report = dict(
            probe="relation-storage", pairs=10416, results=21, publish_s=monotonic() - tick
        )
        assert db.execute("SELECT count(*) FROM relation_pair").fetchone() == (10416,)
        url = f"{ROOT}/workspaces/{w}/snapshots/{s}/relations/{res}"
        observations = []
        for analyzed in (False, True):
            if analyzed:
                db.execute("ANALYZE relation_pair")
                db.execute("ANALYZE relation_result")
            for mode in ("force_generic_plan", "force_custom_plan"):
                with runtime(w, proof) as client:
                    client.execute("SELECT set_config('plan_cache_mode',%s,false)", (mode,))
                    if analyzed:
                        client.execute("SET random_page_cost=1.1")
                    scans = []

                    def observe(notice, scans=scans):
                        _, marker, raw = notice.message_primary.partition("plan:\n")
                        if (
                            not marker
                            or "INTO pairs" in raw
                            or "SELECT coalesce(jsonb_agg(evidence ORDER BY left_id" not in raw
                        ):
                            return
                        nodes = [json.loads(raw)["Plan"]]
                        while nodes:
                            node = nodes.pop()
                            nodes.extend(node.get("Plans", []))
                            if node.get("Relation Name") == "relation_pair":
                                scans.append(
                                    (node["Actual Rows"] + node.get("Rows Removed by Filter", 0))
                                    * node["Actual Loops"]
                                )

                    client.add_notice_handler(observe)
                    for offset in (0, 490):
                        page = client.execute(
                            "SELECT relation_read(%s,%s,%s,%s,%s,NULL,NULL,false)",
                            (w, src, s, res, offset),
                        ).fetchone()[0]
                        assert len(page["pairs"]) == min(10, 496 - offset)
                    assert scans and max(scans) <= 496, scans
                    observations.append(dict(analyzed=analyzed, mode=mode, max_visited=max(scans)))
        report["plans"] = observations
        report["reads"] = []
        for offset in (0, 0, 490):
            tick = monotonic()
            page = web.get(url, params=dict(source_id=str(src), offset=offset))
            page.raise_for_status()
            report["reads"].append(
                dict(offset=offset, seconds=monotonic() - tick, bytes=len(page.content))
            )
            assert len(page.content) <= 65536
        report["pg_backend_hwm_kib"] = linux_peak_kib(reader.info.backend_pid)
        report["parent_hwm_kib"] = linux_peak_kib("self")
        req = ask(web, w, "source", src, "export")
        assert req["report"]["tables"]["relation_pair"] == 10416
        report["payload_bytes"] = req["report"]["relation_payload_bytes"]
        out = tmp_path / "handoff"
        out.mkdir(mode=0o700)
        tick = monotonic()
        operator("export", "--request", req["id"], "--root", root, "--owner-handoff", out)
        report["export_s"] = monotonic() - tick
        path = out / (req["id"] + ".zip")
        report["export_bytes"] = path.stat().st_size
        with ZipFile(path) as z:
            with z.open("rows/relation_pair.jsonl") as file:
                assert sum(1 for _ in file) == 10416
        req = ask(web, w, "source", src, "erase")
        tick = monotonic()
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
        report["erase_s"] = monotonic() - tick
        assert db.execute("SELECT count(*) FROM relation_pair").fetchone() == (0,)
        print(json.dumps(report), flush=True)
