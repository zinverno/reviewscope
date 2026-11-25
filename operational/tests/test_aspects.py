"""Frozen definitions and their candidates, using only the disposable synthetic cabinet."""

import hashlib
import json
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from time import monotonic
from uuid import uuid4
from zipfile import ZipFile

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from operational import worker
from operational.retention import capture_access, recover
from operational.tests.conftest import freeze_objects, generation, prepare_copy
from operational.tests.test_access import COOKIE, CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import FIX, call, command, csv
from operational.tests.test_retention import ask, fresh_restore_cluster, operator
from operational.tests.test_schema import bind_place, scope
from operational.tests.test_worker import cli
from reviewscope.access.http import create_app
from reviewscope.access.ledger import ledger_path, read_witness
from reviewscope.access.operator import provision
from reviewscope.access.security import session_proof

A = {
    "name": "Очередь до оформления заказа",
    "definition": "Ожидание до начала оформления/приёма заказа сотрудником или терминалом. Время приготовления и передачи готового заказа исключено.",
    "include": ["Двадцать минут стояли в очереди к кассе", "Долго никто не принимал заказ"],
    "exclude": [
        "Суп готовили сорок минут",
        "Пакет уже был собран, но его не выдавали",
        "Очереди не было",
    ],
}
B = {
    "name": "Задержка выдачи готового заказа",
    "definition": "Ожидание передачи уже приготовленного или собранного заказа. Задержка до приёма, приготовление и ожидание без установленной готовности исключены.",
    "include": [
        "Еда была готова, но пакет отдали только через двадцать минут",
        "Собранный заказ остывал на стойке",
    ],
    "exclude": [
        "Долго стояли перед кассой",
        "Готовили суп полчаса",
        "Готовый заказ выдали сразу",
        "Ждали сорок минут, неизвестно на каком этапе",
    ],
}


def post(web, path, body, status):
    reply = web.post(path, json=body, headers=CSRF)
    assert reply.status_code == status, (reply.status_code, reply.text)
    return reply.json()


def create(web, path, payload, **kwargs):
    return post(web, path, dict(payload=payload, idempotency_key=str(uuid4()), **kwargs), 201)


def test_aspect_search(db, tmp_path, monkeypatch, key, caplog):  # noqa: F811
    args, root, w, src = command(db, tmp_path)
    p = db.execute("SELECT project_id FROM source WHERE id=%s", (src,)).fetchone()[0]
    _, foreign, _ = scope(db, workspace=w)
    route = f"{ROOT}/workspaces/{w}/projects/{p}/aspects"
    snap = call(args + csv("a") + ["--apply"])["snapshot_id"]
    db.execute(
        "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed) VALUES (%s,%s,%s,true,true)",
        (w, src, hashlib.sha256((FIX / "a.csv").read_bytes()).hexdigest()),
    )
    candidates = f"{ROOT}/workspaces/{w}/snapshots/{snap}/candidates"
    password = secrets.token_urlsafe(32)
    actor = provision(db, "author", password, key, workspace=w, role="analyst")
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert web.get(route).status_code == 401
        assert login(web, "author", password).status_code == 204
        base = dict(payload=B, idempotency_key=str(uuid4()))
        a = create(web, route, A)
        b = post(web, route, base, 201)
        assert a["aspect_id"] != b["aspect_id"]
        assert post(web, route, base, 200) == b
        post(web, route, base | {"payload": A}, 409)
        history = f"{route}/{b['aspect_id']}/versions"
        exact = history + "/" + b["version_id"]
        v1 = web.get(exact).json()
        assert v1["payload"] == B and v1["version_no"] == 1
        canonical = {k: B[k] for k in sorted(B, key=lambda k: (len(k.encode()), k.encode()))}
        assert (
            v1["digest"]
            == hashlib.sha256(
                ("aspect-definition-1\n" + json.dumps(canonical, ensure_ascii=False)).encode()
            ).hexdigest()
        )
        twin = create(web, route, B)
        assert (
            twin["aspect_id"] != b["aspect_id"]
            and web.get(f"{route}/{twin['aspect_id']}/versions/{twin['version_id']}").json()[
                "digest"
            ]
            == v1["digest"]
        )
        assert web.get(exact.replace(str(p), str(foreign))).status_code == 404
        for bad in (
            {"name": " "},
            {"include": ["\x00"]},
            {"exclude": B["include"]},
            {"extra": 1},
            {"definition": 3},
            {"name": "a" * 121},
            {"include": ["🍒" * 495 + str(i) for i in range(10)]},
        ):
            post(web, route, base | {"payload": B | bad}, 422)
        dup = json.dumps(base, ensure_ascii=False).replace(
            '"name":', '"name":"duplicate","name":', 1
        )
        assert (
            web.post(
                route, content=dup.encode(), headers=CSRF | {"Content-Type": "application/json"}
            ).status_code
            == 422
        )
        overview = json.loads(
            Path("prototypes/reviewscope-ui/src/overview.synthetic.json").read_text()
        )
        place = overview["places"][0]
        origin = ":".join(
            (
                overview["snapshot"],
                overview["run"],
                place["source_result"],
                place["id"],
                str(place["topics"][0]["id"]),
            )
        )
        proposal = create(web, route, A, origin=origin)
        post(web, route, base | {"origin": origin + "9"}, 422)
        assert (
            web.get(f"{route}/{proposal['aspect_id']}/versions/{proposal['version_id']}").json()[
                "origin"
            ]
            == origin
        )
        query = dict(
            query="готовый заказ",
            mode="lexical",
            k=3,
            role="query_to_passage",
            source_id=str(src),
            idempotency_key=str(uuid4()),
        )
        legacy = post(web, candidates, query, 202)["id"]
        assert cli("free") == "succeeded"

        def read(run):
            response = web.get(candidates, params={"source_id": src, "run_id": run})
            assert response.status_code == 200, response.text
            return response.json()

        original = read(legacy)
        assert "definition" not in original
        bound = query | dict(aspect=b, idempotency_key=str(uuid4()))
        r1 = post(web, candidates, bound, 202)["id"]
        assert post(web, candidates, bound, 200)["id"] == r1
        post(web, candidates, bound | {"aspect": a}, 409)
        post(web, candidates, query | {"idempotency_key": bound["idempotency_key"]}, 409)
        post(web, candidates, bound | {"aspect": b | {"version_id": a["version_id"]}}, 404)
        sibling = create(web, route.replace(str(p), str(foreign)), B)
        post(web, candidates, bound | {"aspect": sibling}, 404)
        barrier = Barrier(2)

        def edit(n):
            with TestClient(create_app(), base_url=ORIGIN) as editor:
                editor.cookies.update(web.cookies)
                barrier.wait()
                return editor.post(
                    history,
                    headers=CSRF,
                    json=dict(
                        payload=B | {"definition": B["definition"] + f" Уточнение {n}."},
                        expected_version=b["version_id"],
                        idempotency_key=str(uuid4()),
                    ),
                )

        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(edit, range(2)))
        assert sorted(r.status_code for r in replies) == [201, 409]
        b2 = next(r.json() for r in replies if r.status_code == 201)
        assert b2["aspect_id"] == b["aspect_id"] and b2["version_id"] != b["version_id"]
        assert cli("frozen-v1") == "succeeded"
        first = read(r1)
        assert first["query"] == query["query"] and first["definition"]["payload"] == B
        assert first["definition"]["digest"] == v1["digest"]
        assert original["result"]["candidates"] == first["result"]["candidates"]
        obj = root / str(w) / "sha256" / first["artifact_sha256"]
        raw = obj.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == first["artifact_sha256"]
        assert json.loads(raw)["aspect"] == dict(
            **b, project_id=str(p), source_id=str(src), digest=v1["digest"]
        )
        req2 = bound | dict(aspect=b2, idempotency_key=str(uuid4()))
        r2 = post(web, candidates, req2, 202)["id"]
        assert cli("v2") == "succeeded"
        second = read(r2)
        assert (
            second["definition"]["version_no"] == 2
            and second["definition"]["digest"] != v1["digest"]
        )
        assert second["result"]["candidates"] == first["result"]["candidates"]
        src2 = db.execute(
            """INSERT INTO source(workspace_id,project_id,source_key,kind,origin_label,external_source_id)
            VALUES (%s,%s,'second','synthetic_demo','fixture','synthetic') RETURNING id""",
            (w, p),
        ).fetchone()[0]
        for place_id in ("p1", "p2"):
            bind_place(db, w, p, src2, place_id)
        db.execute(
            "INSERT INTO delivery_authorization SELECT workspace_id,%s,operator_login,principal_id FROM delivery_authorization WHERE source_id=%s",
            (src2, src),
        )
        args2 = [str(src2) if x == str(src) else x for x in args]
        snap2 = call(args2 + csv("a") + ["--apply"])["snapshot_id"]
        db.execute(
            "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed) SELECT workspace_id,%s,checksum,true,true FROM browser_admission WHERE source_id=%s",
            (src2, src),
        )
        route2 = candidates.replace(str(snap), snap2)
        moved = post(
            web, route2, req2 | dict(source_id=str(src2), idempotency_key=str(uuid4())), 202
        )["id"]
        assert cli("second-source") == "succeeded"
        moved_result = web.get(route2, params=dict(source_id=src2, run_id=moved)).json()
        assert moved_result["definition"] == second["definition"] and moved_result["result"][
            "source_id"
        ] == str(src2)
        removed = post(web, candidates, bound | dict(idempotency_key=str(uuid4())), 202)["id"]
        assert (
            web.post(f"{ROOT}/workspaces/{w}/runs/{removed}/cancel", headers=CSRF).json()["status"]
            == "cancelled"
        )
        # A mismatched worker projection cannot publish even when its checksum is self-consistent.
        failed = post(web, candidates, bound | dict(idempotency_key=str(uuid4())), 202)["id"]
        original_summary = worker.summarize

        def changed_binding(*args):
            doc = original_summary(*args)
            doc["aspect"] = doc["aspect"] | {"digest": "0" * 64}
            return doc

        with monkeypatch.context() as mp, worker.connect() as conn:
            mp.setattr(worker, "summarize", changed_binding)
            assert (
                worker.process(conn, conn.execute("SELECT * FROM claim_job()").fetchone(), Event())
                == "failed"
            )
        assert db.execute(
            "SELECT reason,output_artifact_id FROM analysis_run WHERE id=%s", (failed,)
        ).fetchone() == ("INTEGRITY_ERROR", None)
        denied = post(web, candidates, bound | dict(idempotency_key=str(uuid4())), 202)["id"]

        def revoke(*args):
            doc = original_summary(*args)
            db.execute(
                "UPDATE browser_admission SET candidate_allowed=false WHERE source_id=%s", (src,)
            )
            return doc

        with monkeypatch.context() as mp, worker.connect() as conn:
            mp.setattr(worker, "summarize", revoke)
            assert (
                worker.process(conn, conn.execute("SELECT * FROM claim_job()").fetchone(), Event())
                == "failed"
            )
        assert db.execute(
            "SELECT reason,output_artifact_id FROM analysis_run WHERE id=%s", (denied,)
        ).fetchone() == ("ACCESS_REVOKED", None)
        assert web.get(candidates, params=dict(source_id=src, run_id=r1)).status_code == 404
        db.execute("UPDATE browser_admission SET candidate_allowed=true WHERE source_id=%s", (src,))
        archive = dict(expected_version=b2["version_id"], idempotency_key=str(uuid4()))
        post(web, history.removesuffix("versions") + "archive", archive, 200)
        assert post(web, candidates, bound, 200)["id"] == r1
        post(web, candidates, bound | {"idempotency_key": str(uuid4())}, 409)
        reopened = read(r1)
        for field in ("definition", "result", "query", "artifact_sha256"):
            assert reopened[field] == first[field]
        assert web.get(exact).json()["archived"]
        assert web.get(history).json()[0]["version_no"] == 2
        witness = ledger_path()
        parked = witness.with_suffix(".parked")
        witness.rename(parked)
        try:
            assert (
                web.get(exact).status_code
                == web.get(candidates, params=dict(source_id=src, run_id=r1)).status_code
                == 503
            )
        finally:
            parked.rename(witness)
        obj.rename(obj.with_suffix(".held"))
        assert web.get(candidates, params={"source_id": src, "run_id": r1}).status_code == 503
        obj.with_suffix(".held").rename(obj)
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        assert (
            web.get(exact).status_code
            == web.get(candidates, params={"source_id": src, "run_id": r1}).status_code
            == 403
        )
        for role in ("rs_runtime", "rs_worker", "rs_upload", "rs_retention"):
            with psycopg.connect(user=role, autocommit=True) as conn:
                for table in ("aspect", "aspect_version"):
                    if role == "rs_retention":
                        assert conn.execute(f"SELECT aspect_id FROM {table}").fetchall() == []
                    else:
                        with pytest.raises(psycopg.errors.InsufficientPrivilege):
                            conn.execute(f"SELECT aspect_id FROM {table}")
        assert B["definition"] not in caplog.text
        with pytest.raises(psycopg.errors.CheckViolation):
            db.execute(
                "UPDATE aspect_version SET payload=%s WHERE version_id=%s",
                (Jsonb(A), b["version_id"]),
            )
        db.execute(
            "UPDATE workspace_membership SET role='analyst' WHERE workspace_id=%s AND principal_id=%s",
            (w, actor),
        )
        mixed = post(web, candidates, query | dict(aspect=a, idempotency_key=str(uuid4())), 202)[
            "id"
        ]
        with worker.connect() as conn:
            lease = conn.execute("SELECT * FROM claim_job()").fetchone()
            db.execute(
                "INSERT INTO source(workspace_id,project_id,source_key,kind,origin_label) VALUES(%s,%s,'mixed','file_export','synthetic test')",
                (w, p),
            )
            assert web.get(exact).status_code == 404
            assert worker.process(conn, lease, Event()) == "failed"
        assert db.execute(
            "SELECT reason,output_artifact_id FROM analysis_run WHERE id=%s", (mixed,)
        ).fetchone() == ("ACCESS_REVOKED", None)


def test_aspect_catalog_bounds(db, key):  # noqa: F811
    w, p, _ = scope(db)
    password = secrets.token_urlsafe(32)
    provision(db, "catalog", password, key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "catalog", password).status_code == 204
        route = f"{ROOT}/workspaces/{w}/projects/{p}/aspects"
        with psycopg.connect(user="rs_runtime") as conn:
            conn.execute(
                "SELECT set_config('rs.workspace',%s,true),set_config('rs.session_proof',%s,true)",
                (str(w), session_proof(web.cookies[COOKIE], key)),
            )
            for _ in range(100):
                a = v = None
                for _ in range(10):
                    status, a, v = conn.execute(
                        "SELECT * FROM save_aspect(%s,%s,%s,%s,%s,%s,'manual',false)",
                        (w, p, a, v, uuid4(), Jsonb(A)),
                    ).fetchone()
                    assert status == 201
        db.execute("ANALYZE aspect, aspect_version")
        started = monotonic()
        pages = [web.get(route, params={"offset": n}) for n in (0, 25, 50, 75, 100)]
        assert [len(r.json()) for r in pages] == [25, 25, 25, 25, 0]
        assert len({item["aspect_id"] for r in pages for item in r.json()}) == 100
        detail = web.get(f"{route}/{a}/versions/{v}")
        assert detail.json()["version_no"] == 10
        plan = db.execute(
            "EXPLAIN (ANALYZE,FORMAT JSON) SELECT payload FROM aspect_version WHERE workspace_id=%s AND project_id=%s AND aspect_id=%s AND version_id=%s",
            (w, p, a, v),
        ).fetchone()[0][0]
        assert "Index" in plan["Plan"]["Node Type"]
        print(
            json.dumps(
                dict(
                    probe="aspect-catalog",
                    pages_ms=round((monotonic() - started) * 1000, 1),
                    max_page_bytes=max(len(r.content) for r in pages),
                    detail_bytes=len(detail.content),
                    lookup=plan["Plan"]["Node Type"],
                )
            )
        )


@pytest.mark.parametrize("kind", ["source", "project", "workspace"])
def test_aspect_restore(db, tmp_path, monkeypatch, key, kind):  # noqa: F811
    args, root, w, src = command(db, tmp_path)
    p = db.execute("SELECT project_id FROM source WHERE id=%s", (src,)).fetchone()[0]
    snap = call(args + csv("a") + ["--apply"])["snapshot_id"]
    db.execute(
        "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed) VALUES (%s,%s,%s,true,true)",
        (w, src, hashlib.sha256((FIX / "a.csv").read_bytes()).hexdigest()),
    )
    _, sibling, _ = scope(db, workspace=w)
    password = secrets.token_urlsafe(32)
    provision(db, "custodian", password, key, workspace=w)
    route = f"{ROOT}/workspaces/{w}/projects/{p}/aspects"
    with TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "custodian", password).status_code == 204
        other = create(web, route.replace(str(p), str(sibling)), B)
        saved = create(web, route, B)
        request = dict(
            source_id=str(src),
            aspect=saved,
            idempotency_key=str(uuid4()),
            query="готов",
            role="query_to_passage",
            mode="lexical",
            k=1,
        )
        candidate = f"{ROOT}/workspaces/{w}/snapshots/{snap}/candidates"
        rid = post(web, candidate, request, 202)["id"]
        assert cli("archive-bound") == "succeeded"
        with importer(tmp_path, monkeypatch):
            rr = post(
                web,
                f"{candidate}/{rid}/rerank",
                dict(
                    source_id=str(src),
                    idempotency_key=str(uuid4()),
                    profile="mmarco-context-logit-v1",
                ),
                202,
            )["id"]
            with monkeypatch.context() as mp:
                mp.delenv("RS_RERANK_MODELS", raising=False)
                assert cli("archive-rerank")=="succeeded"
        base = f"{route}/{saved['aspect_id']}"
        v2 = post(
            web,
            base + "/versions",
            dict(payload=A, idempotency_key=str(uuid4()), expected_version=saved["version_id"]),
            201,
        )
        post(
            web,
            base + "/archive",
            dict(expected_version=v2["version_id"], idempotency_key=str(uuid4())),
            200,
        )
        selected = {"source": src, "project": p, "workspace": w}[kind]
        req = ask(web, w, kind, selected, "export")
        out = tmp_path / "handoff"
        out.mkdir(mode=0o700)
        operator("export", "--request", req["id"], "--root", root, "--owner-handoff", out)
        with ZipFile(out / (req["id"] + ".zip")) as z:
            rows = [json.loads(line) for line in z.read("rows/aspect_version.jsonl").splitlines()]
            assert [r["payload"] for r in rows if r["aspect_id"] == saved["aspect_id"]] == (
                [] if kind == "source" else [B, A]
            )
            assert sum(r["aspect_id"] == other["aspect_id"] for r in rows) == int(
                kind == "workspace"
            )
            run = next(
                json.loads(line)
                for line in z.read("rows/analysis_run.jsonl").splitlines()
                if json.loads(line)["id"] == rid
            )
            derivative = next(
                json.loads(line)
                for line in z.read("rows/analysis_run.jsonl").splitlines()
                if json.loads(line)["id"] == rr
            )
            assert derivative["candidate_params"]["parent_run_id"] == rid
            assert hashlib.sha256(z.read("objects/"+derivative["candidate_params"]["parent_sha256"])).hexdigest()==derivative["candidate_params"]["parent_sha256"]
            assert run["candidate_params"]["aspect"]["version_id"] == saved["version_id"]
        archive = tmp_path / "before.dump"
        subprocess.run(["pg_dump", "-Fc", "-f", archive], check=True)
        frozen = freeze_objects(root, tmp_path, [w])
        req = ask(web, w, kind, selected, "erase")
        assert (
            req["report"]["tables"]["aspect_version"]
            == {"source": 0, "project": 2, "workspace": 3}[kind]
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
        proof, seal = read_witness()["sha256"], capture_access(db)
        with (
            fresh_restore_cluster(monkeypatch, archive) as (_, name),
            psycopg.connect(autocommit=True) as target,
        ):
            target.execute(
                sql.SQL("REVOKE TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
            )
            assert target.execute("SELECT count(*) FROM aspect_version").fetchone() == (3,)
            dest = generation("aspects")
            token = prepare_copy(target, frozen, dest, proof, seal)
            recover(target, proof, [token], seal)
            assert target.execute(
                "SELECT version_no FROM aspect_version WHERE aspect_id=%s ORDER BY version_no",
                (saved["aspect_id"],),
            ).fetchall() == ([(1,), (2,)] if kind == "source" else [])
            assert target.execute(
                "SELECT count(*) FROM aspect_version WHERE aspect_id=%s", (other["aspect_id"],)
            ).fetchone() == (int(kind != "workspace"),)
            assert target.execute(
                "SELECT count(*) FROM analysis_run WHERE bound_version IS NOT NULL"
            ).fetchone() == (0,)
