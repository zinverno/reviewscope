"""Full vs incremental on the same admitted delivery. All runs use the deployed lexical scorer."""

import hashlib
import json
import os
from pathlib import Path
from threading import Event
from time import monotonic, process_time
from uuid import uuid4

import pytest

from operational import worker
from operational.tests.monitor_resource_fixture import resource_pair
from operational.tests.test_access import key  # noqa: F401
from operational.tests.test_decision_storage import cabinet, enqueue, page
from operational.tests.test_deliveries import call
from reviewscope.aspect_decisions import definitions


@pytest.mark.parametrize(
    "profile",
    ["small"]
    + (["repeated-10k", "diverse-4mib-10k"] if os.getenv("RS_MEASURE_MONITORS") == "1" else []),
)
def test_monitor_resource(db, tmp_path, key, profile):  # noqa: F811
    with (
        cabinet(db, tmp_path, key, definition=definitions()["queue"]) as (s, reader, _, root, _, p),
        worker.connect() as conn,
    ):
        w, src, _, aspect, version = s
        if profile == "small":
            from operational.tests.test_deliveries import csv

            inputs = [csv("a"), csv("b")]
        else:
            inputs = list(resource_pair(tmp_path, profile))
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
        saved = reader.execute(
            "SELECT monitor_save(%s,%s,%s,NULL,NULL,%s,%s,'resource',1,1,'accepted_in_delivery',false)",
            (w, p, src, aspect, version),
        ).fetchone()[0]
        m, v = saved["monitor_id"], saved["version_id"]

        def admit(argv):
            result = call([*args, *argv, "--apply"])
            file = Path(argv[argv.index("--input") + 1])
            db.execute(
                """INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,decision_allowed,monitor_allowed)
                       VALUES (%s,%s,%s,true,true,true,true) ON CONFLICT DO NOTHING""",
                (w, src, hashlib.sha256(file.read_bytes()).hexdigest()),
            )
            return result["snapshot_id"]

        a, b = map(admit, inputs)
        samples = []

        def execute(snap, tag, parent=None, ordinary=False):
            tic, cpu = monotonic(), process_time()
            if ordinary:
                code, did = enqueue(reader, (w, src, snap, aspect, version))
                assert code == 202
                rid = None
            else:
                res = reader.execute(
                    "SELECT monitor_start(%s,%s,%s,%s,%s,%s,%s,%s)",
                    (w, p, src, m, v, snap, parent, uuid4()),
                ).fetchone()[0]
                assert res["code"] == 202, res
                rid, did = res["run_id"], res["decision_run_id"]
            job = conn.execute("SELECT * FROM claim_job()").fetchone()
            assert str(job[1]) == str(did)
            outcome = worker.process(conn, job, Event())
            assert outcome == "succeeded", db.execute(
                "SELECT reason FROM analysis_run WHERE id=%s", (did,)
            ).fetchone()
            wall = monotonic() - tic
            scope = (w, src, snap, aspect, version)
            first = page(reader, scope, did)
            latency = []
            for offset in (0, 0, max(0, first["filtered_total"] - 25)):
                start = monotonic()
                section = page(reader, scope, did, skip=offset)
                latency.append(
                    (monotonic() - start, len(json.dumps(section, ensure_ascii=False).encode()))
                )
                assert latency[-1][0] < 12 and latency[-1][1] <= 65536
            samples.append(
                dict(
                    profile=profile,
                    tag=tag,
                    wall_s=wall,
                    parent_cpu_s=process_time() - cpu,
                    rows_sha=first["rows_sha256"],
                    accepted=first["summary"]["N_accepted"],
                    eligible=first["summary"]["N_eligible"],
                    page_seconds_bytes=latency,
                    artifact_bytes=db.execute(
                        "SELECT size_bytes FROM artifact WHERE run_id=%s", (did,)
                    ).fetchone()[0],
                )
            )
            return rid, first

        first_id, first = execute(a, "full-A")
        _, control = execute(b, "independent-full-B", ordinary=True)
        rid, inc = execute(b, "incremental-B", first_id)
        assert inc["rows_sha256"] == control["rows_sha256"] and inc["summary"] == control["summary"]
        c_args = inputs[0].copy()
        c_args[c_args.index("--delivery-id") + 1] = str(uuid4())
        c = admit(c_args)
        _, identical = execute(c, "stable-repeat", first_id)
        assert identical["rows_sha256"] == first["rows_sha256"]
        if profile != "small":
            receipt = db.execute(
                "SELECT document FROM monitor_receipt WHERE document->>'run_id'=%s", (rid,)
            ).fetchone()[0]
            assert receipt["categories"] == dict(
                unchanged_version=9000,
                changed_version=400,
                newly_observed=400,
                not_observed_in_target=600,
                unlinked_observation=200,
            )
            assert receipt["reused_eligible"] == 9000 and receipt["computed_eligible"] == 1000
        print(json.dumps({"monitor_measurements": samples}), flush=True)
