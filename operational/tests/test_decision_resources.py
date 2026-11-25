"""Measure the deployed decision profile on public synthetic inputs in a disposable cluster."""

import csv as csvlib
import hashlib
import json
import os
import secrets
import subprocess
import sys
from collections import Counter
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from operational.decision_worker import PROFILE
from operational.tests.test_access import ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_aspects import A, create, post
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import call, command, conflicting_json, csv, inputs
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision
from reviewscope.import_preview.contract import MAX_BYTES, MAX_ROWS

SMALL = ["a", "b", "problematic", "context"]
LARGE = ["public-demo", "repeated-10k", "diverse-10k"]


def resource_input(directory, name):
    if name in ("a", "b"):
        return csv(name)
    if name == "problematic":
        return conflicting_json(directory)
    file = directory / "input.csv"
    if name == "repeated-10k":
        header = b"id,body\n"
        width, extra = divmod(MAX_BYTES - len(header) - MAX_ROWS * len(b"00000,\n"), MAX_ROWS)
        with file.open("wb") as out:
            out.write(header)
            for i in range(MAX_ROWS):
                out.write(b"%05d,%s\n" % (i, b"t" * (width + (i < extra))))
        assert file.stat().st_size == MAX_BYTES
    else:
        if name == "public-demo":
            import duckdb

            from operational.backfill_demo import DEMO_SHA

            path = Path("demo/reviewscope_demo.duckdb")
            with path.open("rb") as raw:
                assert hashlib.file_digest(raw, "sha256").hexdigest() == DEMO_SHA
            with duckdb.connect(str(path), read_only=True) as frozen:
                texts = [
                    r[0]
                    for r in frozen.execute(
                        "SELECT text FROM reviews ORDER BY review_id"
                    ).fetchall()
                ]
        elif name == "diverse-10k":
            from scripts.profile_rerank import reviews

            texts = reviews()
        else:
            same = "☕ e\u0301 У кассы пришлось долго стоять.\r\nПотом приняли заказ."
            texts = [
                "",
                same,
                same,
                "Никакой очереди. Приготовили быстро, готовый пакет не отдавали.",
                "В комнате стоял стол. " * 300 + "До кассы добрались только через полчаса.",
            ]
        with file.open("w", newline="", encoding="utf8") as out:
            writer = csvlib.writer(out)
            writer.writerow(("id", "body"))
            writer.writerows(enumerate(texts))
    mapping = directory / "mapping"
    mapping.write_text('{"version":"resource-1","fields":{"id":"source_review_id","body":"text"}}')
    passport = directory / "passport"
    passport.write_text('{"delimiter":",","source":"synthetic","source_place_id":"p1"}')
    return inputs(file, mapping, passport, "csv")


@pytest.mark.parametrize(
    "fixture", SMALL + (LARGE if os.getenv("RS_MEASURE_DECISIONS") == "1" else [])
)
def test_decision_profile_resource(db, tmp_path, monkeypatch, key, fixture):  # noqa: F811
    args, _, w, src = command(db, tmp_path)
    payload = resource_input(tmp_path, fixture)
    file = Path(payload[payload.index("--input") + 1])
    expected = {
        "a": (3, 3),
        "b": (6, 6),
        "problematic": (12, 6),
        "context": (5, 5),
        "public-demo": (1058, 1058),
        "repeated-10k": (10000, 10000),
        "diverse-10k": (10000, 10000),
    }
    delivery = call(args + payload + ["--apply"])
    assert (delivery["input"], delivery["accepted"]) == expected[fixture]
    snap = delivery["snapshot_id"]
    db.execute(
        """INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,decision_allowed)
               VALUES(%s,%s,%s,true,true,true)""",
        (w, src, hashlib.sha256(file.read_bytes()).hexdigest()),
    )
    project = db.execute("SELECT project_id FROM source WHERE id=%s", (src,)).fetchone()[0]
    secret = secrets.token_urlsafe(24)
    provision(db, "decision-resource", secret, key, workspace=w, role="analyst")
    path = f"{ROOT}/workspaces/{w}"
    route = f"{path}/snapshots/{snap}/decisions"
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as client:
        assert login(client, "decision-resource", secret).status_code == 204
        definition = create(client, f"{path}/projects/{project}/aspects", A)
        run = post(
            client,
            route,
            dict(**definition, source_id=str(src), profile=PROFILE, idempotency_key=str(uuid4())),
            202,
        )["id"]
        started = monotonic()
        job = subprocess.run(
            [sys.executable, "-m", "operational.worker", "--once"],
            capture_output=True,
            timeout=220,
            check=True,
        )
        seconds = monotonic() - started
        assert not job.stderr
        events = [json.loads(line) for line in job.stdout.splitlines()]
        params = dict(source_id=src)
        tick = monotonic()
        response = client.get(f"{route}/{run}", params=params)
        response.raise_for_status()
        first_s = monotonic() - tick
        doc = response.json()
        summary = doc.get("summary")
        envelope = dict(
            probe="decision-profile",
            fixture=fixture,
            status=doc["status"],
            reason=doc.get("reason"),
            processed=doc["processed"],
            eligible=doc["total"],
            seconds=seconds,
            events=events,
            input_bytes=file.stat().st_size,
            first_s=first_s,
            first_bytes=len(response.content),
        )
        if doc["reason"] == "RESOURCE_LIMIT" and fixture in LARGE:
            assert doc["status"] == "failed" and summary is None and doc["rows"] == []
            print(json.dumps(envelope))
            return
        assert doc["status"] == "succeeded", envelope
        # Independent accounting from the original snapshot, with one observed key per accepted version.
        originals = db.execute(
            """SELECT m.version_id,v.text_content FROM snapshot_member m JOIN review_version v
                ON (m.workspace_id,m.source_id,m.version_id)=(v.workspace_id,v.source_id,v.id)
                WHERE (m.workspace_id,m.snapshot_id)=(%s,%s) ORDER BY m.version_id""",
            (w, snap),
        ).fetchall()
        rows = db.execute(
            """SELECT version_id,outcome::text,text_sha256,"start","end" FROM decision_row
                             WHERE (workspace_id,run_id)=(%s,%s) ORDER BY version_id""",
            (w, run),
        ).fetchall()
        counts = Counter()
        for (version, text), (row_id, label, sha, start, end) in zip(originals, rows, strict=True):
            text = text or ""
            assert version == row_id and hashlib.sha256(text.encode()).hexdigest() == sha
            assert (label == "excluded_blank") == (not text.strip())
            assert (start is end is None) or (0 <= start < end <= len(text))
            counts[label] += 1
        eligible = len(rows) - counts["excluded_blank"]
        complaints = counts["complaint"] + counts["mixed"]
        mentions = eligible - counts["not_mentioned"] - counts["topic_uncertain"]
        assert (
            summary["N_accepted"],
            summary["N_eligible"],
            summary["N_evaluated"],
            summary["N_complaints"],
            summary["N_mentioned"],
            summary["N_not_evaluated"],
        ) == (len(originals), eligible, eligible, complaints, mentions, 0)
        assert summary["complaints_among_eligible"] == pytest.approx(
            complaints / eligible if eligible else None
        )
        assert summary["complaints_among_mentions"] == pytest.approx(
            complaints / mentions if mentions else None
        )
        offset = max(0, (len(rows) - 1) // 25 * 25)
        tick = monotonic()
        last = client.get(f"{route}/{run}", params=params | dict(offset=offset))
        last.raise_for_status()
        last_s = monotonic() - tick
        assert [r["version_id"] for r in last.json()["rows"]] == [str(r[0]) for r in rows[offset:]]
        assert max(len(last.content), len(response.content)) <= 65536
        size = db.execute("SELECT size_bytes FROM artifact WHERE run_id=%s", (run,)).fetchone()[0]
        print(
            json.dumps(
                dict(
                    **envelope,
                    last_s=last_s,
                    last_bytes=len(last.content),
                    artifact_bytes=size,
                    counts=counts,
                    summary=summary,
                ),
                default=float,
            )
        )
