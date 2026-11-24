"""Search admitted deliveries through the real runtime, worker and object broker."""

import hashlib
import secrets
from uuid import uuid4

from fastapi.testclient import TestClient

from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import FIX, call, command, csv, json_input
from operational.tests.test_worker import cli
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision


def test_candidate_search(db, tmp_path, monkeypatch, key, caplog):  # noqa: F811
    base, root, w, source = command(db, tmp_path)
    password = secrets.token_urlsafe(20)
    actor = provision(db, "retrieval-owner", password, key, workspace=w)
    path = f"{ROOT}/workspaces/{w}"
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "retrieval-owner", password).status_code == 204

        def enqueue(route, body, status=202):
            res = web.post(route, json=body, headers=CSRF)
            assert res.status_code == status, (res.status_code, res.text)
            return res.json().get("id")

        def submit(route, query, mode="lexical"):
            body = dict(
                source_id=str(source),
                query=query,
                idempotency_key=str(uuid4()),
                role="query_to_passage",
                mode=mode,
                k=3,
            )
            run = enqueue(route, body)
            assert enqueue(route, body, 200) == run
            enqueue(route, body | {"query": "Другая формулировка"}, 409)
            assert cli("candidate-search") == "succeeded"
            args = dict(source_id=str(source), run_id=run)
            result = web.get(route, params=args)
            assert result.status_code == 200, result.text
            return result.json(), args

        def register(args, file):
            snap = call(base + args + ["--apply"])["snapshot_id"]
            route = path + f"/snapshots/{snap}/candidates"
            sha = hashlib.sha256(file.read_bytes()).hexdigest()
            db.execute("INSERT INTO browser_admission VALUES(%s,%s,%s,true)", (w, source, sha))
            assert web.get(route, params={"source_id": source}).status_code == 404
            db.execute(
                "UPDATE browser_admission SET candidate_allowed=true WHERE (workspace_id,source_id,checksum)=(%s,%s,%s)",
                (w, source, sha),
            )
            return route

        for name, n in (("a", 3), ("b", 6)):
            route = register(csv(name), FIX / (name + ".csv"))
            reply, _ = submit(route, "заказ")
            doc = reply["result"]
            assert doc["reviewed_count"] == n
            if name == "a":
                historical = route
        # Two anonymous observations remain distinct; conflicting rows and the confirmed repeat never enter.
        conflict = [
            {"id": "c", "t": "один"},
            {"id": "c", "t": "один"},
            {"id": "c", "t": "два"},
            {"id": "repeat", "t": "кофе"},
            {"id": "repeat", "t": "кофе"},
            {"t": "кофе"},
            {"t": "кофе"},
            {"id": "x", "t": "кофе"},
            {"id": "y", "t": "чай"},
            {"id": "z", "t": "чай"},
            False,
            {"id": 3},
        ]
        route = register(
            json_input(tmp_path, conflict, {"id": "source_review_id", "t": "text"}),
            tmp_path / "data",
        )
        result, _ = submit(route, "кофе")
        assert (
            result["result"]["reviewed_count"] == 6
            and result["result"]["retrieved_review_count"] == 3
        )
        texts = [
            "😀 Кофе вкусный. Не пришлось ждать. Уже готовый пакет всё же унесли на склад.",
            "Чек за салат был пробит дважды.",
            " ",
            "Чек за салат был пробит дважды.",
            "Полка в зале. " * 100 + "В самом конце пропал зонтик.",
        ]
        args = json_input(tmp_path, [{"t": t} for t in texts], {"t": "text"})
        route = register(args, tmp_path / "data")
        result, params = submit(route, "пробит дважды")
        doc = result["result"]
        assert (doc["reviewed_count"], doc["eligible_review_count"]) == (5, 4)
        hits = doc["candidates"]
        assert len({h["version_id"] for h in hits[:2]}) == 2
        assert web.get(historical, params=params).status_code == 404
        assert web.get(route, params=params | {"source_id": uuid4()}).status_code == 404
        for query, needle in (
            ("пропал зонтик", "В самом конце пропал зонтик."),
            ("не пришлось ждать", "Не пришлось ждать."),
        ):
            result, params = submit(route, query)
            hit = result["result"]["candidates"][0]
            detail = web.get(
                route.removesuffix("candidates") + "reviews/" + hit["version_id"],
                params={"source_id": source},
            )
            assert detail.status_code == 200
            original = detail.json()["rows"][0]["full_text"]
            assert (
                original[hit["start"] : hit["end"]] == needle
                and hashlib.sha256(original.encode()).hexdigest() == hit["text_sha256"]
            )
            print("authored span", needle, hit["version_id"], hit["start"], hit["end"], hit["rank"])
        fragment = params | {"fragment_id": hit["fragment_id"]}
        assert (
            web.get(route, params=fragment).json()["evidence"]["rows"][0]["full_text"] == original
        )
        assert web.get(route, params=params | {"fragment_id": "0" * 64}).status_code == 404
        dense, _ = submit(route, "Деньги за одну покупку взяли повторно", "hybrid")
        semantic = dense["result"]
        if semantic["status"] == "READY":
            assert any("dense" in h["channels"] for h in semantic["candidates"])
            assert semantic["index"]["dense"]["dim"] == 384
            assert semantic["retrieved_review_count"] == 3
        else:
            assert semantic["status"] in {"NOT_RUN_MISSING_BACKEND", "NOT_RUN_MISSING_WEIGHTS"}
        artifact = root / str(w) / "sha256" / result["artifact_sha256"]
        parked = artifact.with_suffix(".parked")
        artifact.rename(parked)
        assert web.get(route, params=params).status_code == 503
        parked.rename(artifact)
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        assert web.get(route, params=params).status_code == 403
        db.execute(
            "UPDATE workspace_membership SET role='owner' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        private_marker = "CANARY-private-query-43x"
        invalid = dict(
            source_id=str(source),
            idempotency_key=str(uuid4()),
            query=private_marker,
            mode="invalid",
            role="text_to_text",
            k=1,
        )
        assert web.post(route, json=invalid, headers=CSRF).status_code == 422
        assert web.post(route, json=invalid).status_code == 403
        assert private_marker not in caplog.text
        pending = enqueue(
            route,
            dict(
                source_id=str(source),
                idempotency_key=str(uuid4()),
                query="пакет",
                mode="lexical",
                role="query_to_passage",
                k=1,
            ),
        )
        from threading import Event

        from operational import worker

        original_summary = worker.summarize

        def revoke_before_publication(*args):
            value = original_summary(*args)
            db.execute(
                "UPDATE browser_admission SET candidate_allowed=false WHERE workspace_id=%s", (w,)
            )
            return value

        monkeypatch.setattr(worker, "summarize", revoke_before_publication)
        object_dir = root / str(w) / "sha256"
        before = set(object_dir.iterdir())
        with worker.connect() as conn:
            claim = conn.execute("SELECT * FROM claim_job()").fetchone()
            assert worker.process(conn, claim, Event()) == "failed"
        assert set(object_dir.iterdir()) == before
        assert web.get(path + f"/runs/{pending}/status").json()["reason"] == "ACCESS_REVOKED"
        assert web.get(route, params=params).status_code == 404


def measure_boundary(db, tmp_path, monkeypatch, w, source, snapshot, password):
    import json
    import os
    import subprocess
    import sys
    from time import monotonic

    db.execute(
        "UPDATE browser_admission SET candidate_allowed=true WHERE workspace_id=%s AND source_id=%s",
        (w, source),
    )
    route = f"{ROOT}/workspaces/{w}/snapshots/{snapshot}/candidates"
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "context-boundary", password).status_code == 204
        modes = (
            ["lexical", "dense", "hybrid"]
            if os.environ.get("RS_MEASURE_DENSE") == "1"
            else ["lexical"]
        )
        for mode in modes:
            body = dict(
                source_id=str(source),
                query="xxxxxxxx",
                idempotency_key=str(uuid4()),
                role="query_to_passage",
                mode=mode,
                k=10,
            )
            job = web.post(route, json=body, headers=CSRF)
            assert job.status_code == 202
            start = monotonic()
            worker = subprocess.run(
                [sys.executable, "-m", "operational.worker", "--once"],
                capture_output=True,
                timeout=240,
                check=True,
            )
            events = [json.loads(line) for line in worker.stdout.splitlines()]
            args = dict(source_id=source, run_id=job.json()["id"])
            status = web.get(f"{ROOT}/workspaces/{w}/runs/{args['run_id']}/status").json()
            print(
                json.dumps(
                    dict(
                        probe="candidate-10k",
                        mode=mode,
                        seconds=monotonic() - start,
                        events=events,
                        status=status["status"],
                        reason=status["reason"],
                    )
                )
            )
            if mode != "lexical" and status["reason"] == "RESOURCE_LIMIT":
                assert not status["artifact_id"]
                continue
            assert status["status"] == "succeeded"
            reply = web.get(route, params=args)
            assert reply.status_code == 200, reply.status_code
            result = reply.json()
            doc = result["result"]
            size = db.execute(
                "SELECT size_bytes FROM artifact WHERE checksum=%s", (result["artifact_sha256"],)
            ).fetchone()[0]
            print(
                json.dumps(
                    dict(
                        probe="candidate-10k-result",
                        mode=mode,
                        bytes=size,
                        diagnostics=next(e for e in events if e["event"] == "candidate_resources"),
                        fragments=doc["fragment_count"],
                    )
                )
            )
            assert doc["reviewed_count"] == doc["eligible_review_count"] == 10000
            assert doc["retrieved_review_count"] == 10 and size <= 65536
