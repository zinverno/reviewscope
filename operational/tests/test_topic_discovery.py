"""Canonical synthetic imports and immutable topic decisions in the disposable PostgreSQL cluster."""

import json
import secrets
import subprocess
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from csv import DictReader
from hashlib import sha256
from itertools import combinations
from pathlib import Path
from time import monotonic
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational.tests.conftest import ALEMBIC, new_db
from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_deliveries import COVER, call, command, json_input
from operational.tests.test_human_review import annotation, grant, operation, oracle, submit
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision

MIGRATION = Path("operational/migrations/versions/0019_topic_discovery.sql")


def test_topic_discovery_transactional_upgrade(monkeypatch):
    monkeypatch.setenv("PGDATABASE", new_db())
    subprocess.run([*ALEMBIC, "upgrade", "0018_typed_relations"], check=True)
    with psycopg.connect("", autocommit=True) as db:
        old_seal = db.execute("SELECT retention_access_seal()").fetchone()
        with db.transaction(force_rollback=True):
            db.execute(MIGRATION.read_text())
            assert db.execute("SELECT count(*) FROM topic_result").fetchone() == (0,)
        assert db.execute("SELECT to_regclass('topic_result')").fetchone() == (None,)
        subprocess.run([*ALEMBIC, "upgrade", "head"], check=True)
        assert db.execute("SELECT retention_access_seal()").fetchone() == old_seal


def dated_snapshot(db, tmp_path, args, w, src, month, texts, coverage="full"):
    area = tmp_path / ("period-" + month + "-" + uuid4().hex)
    area.mkdir()
    prefix = "2026-" + month
    (tmp_path / "coverage").write_text(
        json.dumps(
            COVER
            | {
                "coverage": coverage,
                "basis": "published",
                "mode": "period",
                "filters": {},
                "covered_from": prefix + "-01T00:00:00Z",
                "covered_until": prefix + "-28T00:00:00Z",
            }
        )
    )
    rows = [
        {"id": area.name + str(n), "body": t, "at": prefix + "-05T12:00:00Z"}
        for n, t in enumerate(texts)
    ]
    flags = json_input(
        area,
        rows,
        {"id": "source_review_id", "body": "text", "at": "published_at"},
        {"source": "synthetic", "source_place_id": "p1", "event_basis": "published"},
    )
    snap = call(args + flags + ["--apply"])["snapshot_id"]
    db.execute(
        "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,candidate_allowed,topic_discovery_allowed) VALUES (%s,%s,%s,true,true,true)",
        (w, src, sha256((area / "data").read_bytes()).hexdigest()),
    )
    return snap


def test_topic_discovery_holdout_and_history(db, tmp_path, key, monkeypatch):  # noqa: F811
    args, _, w, src = command(db, tmp_path)
    with Path("tests/fixtures/topic-discovery.tsv").open() as source:
        corpus = list(DictReader(source, delimiter="\t"))
    gold = {r["text"]: r["gold"] for r in corpus if r["split"] == "test"}
    hist = dated_snapshot(
        db, tmp_path, args, w, src, "03", [r["text"] for r in corpus if r["split"] == "history"]
    )
    current = dated_snapshot(db, tmp_path, args, w, src, "04", list(gold))
    secret = secrets.token_urlsafe(24)
    provision(db, "topic-check", secret, key, workspace=w, role="analyst")
    route = f"{ROOT}/workspaces/{w}/snapshots/{current}/topic-discovery"
    q = {"source_id": str(src)}
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "topic-check", secret).status_code == 204
        start = operation(src)
        tick = monotonic()
        result = submit(web, route, start)["id"]
        elapsed = monotonic() - tick
        assert submit(web, route, start, 200)["id"] == result
        assert submit(web, route, operation(src))["id"] == result
        url = f"{route}/{result}"

        def page(**params):
            reply = web.get(url, params=q | params)
            assert reply.status_code == 200, reply.text
            return reply.json()

        data = page()
        assert data["manifest"]["N_accepted"] == len(gold)
        predicted = {}
        for candidate in data["rows"]:
            detail = page(candidate_id=candidate["id"])
            for member in detail["rows"]:
                evidence = page(candidate_id=candidate["id"], version=member["version_id"])[
                    "evidence"
                ]["text"]
                assert (
                    sha256(evidence.encode()).hexdigest() == member["text_sha"]
                    and len(evidence) == member["end"]
                )
                predicted[evidence] = candidate["content_key"]
        counts = Counter()
        per_topic = Counter()
        for a, b in combinations([t for t in gold if gold[t] not in {"mixed", "unknown"}], 2):
            same = gold[a] == gold[b] and gold[a] not in {"noise", "denial", "praise"}
            joined = a in predicted and predicted[a] == predicted.get(b)
            counts[(same, joined)] += 1
            if same:
                per_topic[(gold[a], joined)] += 1
        memberships = {}
        for t, cluster in predicted.items():
            if gold[t] not in {"mixed", "unknown"}:
                memberships.setdefault(cluster, Counter())[gold[t]] += 1
        positives = {
            label
            for label in gold.values()
            if label not in {"mixed", "unknown", "noise", "denial", "praise"}
        }
        grouped = [list(v for v in predicted if predicted[v] == cluster) for cluster in memberships]
        pure = sum(max(b.values()) for b in memberships.values())
        majority_n = sum(sum(b.values()) for b in memberships.values())
        covered = sorted(
            label for label in positives if any(b[label] >= 3 for b in memberships.values())
        )
        false_ab = sum(
            sum(gold[t] == "queue" for t in group) * sum(gold[t] == "handover" for t in group)
            for group in grouped
        )
        print(
            "SYNTHETIC_HOLDOUT",
            json.dumps(
                dict(
                    n=len(gold),
                    clustered=len(predicted),
                    candidates=len(data["rows"]),
                    purity=[pure, majority_n],
                    covered=covered,
                    false_queue_handover=false_ab,
                    memberships=sorted(
                        [dict(sorted(b.items())) for b in memberships.values()], key=str
                    ),
                    tp=counts[True, True],
                    fp=counts[False, True],
                    fn=counts[True, False],
                    excluded=sum(x in {"mixed", "unknown"} for x in gold.values()),
                    per_topic={str(k): v for k, v in per_topic.items()},
                    publish_s=elapsed,
                ),
                sort_keys=True,
            ),
        )
        assert data["manifest"]["N_clustered"] == len(predicted)
        assert data["manifest"]["N_noise"] == len(gold) - len(predicted)
        # Explicit history whose wording differs from the current texts.
        hist2 = dated_snapshot(
            db,
            tmp_path,
            args,
            w,
            src,
            "02",
            ["Из поезда написал: крышка стакана протекает.", "Без замечаний."],
        )
        cand = next(x for x in data["rows"] if x["term"] == "крышка стакана")

        def check(historical):
            submit(
                web,
                url + "/history-checks",
                operation(src, candidate_id=cand["id"], historical_snapshot=historical),
            )
            return page(candidate_id=cand["id"], historical_snapshot=historical)["history"][0]

        old = check(hist2)
        assert old["receipt"]["state"] == "HISTORY_CHECKED" and old["receipt"]["N_searched"] == 2
        hit = old["receipt"]["hits"][0]
        assert page(candidate_id=cand["id"], historical_snapshot=hist2, version=hit["version_id"])[
            "evidence"
        ]["text"].startswith("Из поезда")
        verdict = operation(
            src,
            candidate_id=cand["id"],
            historical_snapshot=hist2,
            expected_check=old["id"],
            version=hit["version_id"],
            accept=True,
        )
        submit(web, url + "/history-checks", verdict)
        assert (
            page(candidate_id=cand["id"], historical_snapshot=hist2)["history"][0]["receipt"][
                "state"
            ]
            == "OBSERVED_IN_SELECTED_HISTORY"
        )
        none = check(hist)
        assert none["receipt"]["state"] == "HISTORY_CHECKED" and none["receipt"]["N_searched"] == 5
        assert none["receipt"]["hits"] == []
        # Run the accepted lexical classifier, including its known missed queue complaints.
        from operational.decision_worker import PROFILE
        from operational.tests.test_aspects import A, create, post
        from operational.tests.test_worker import cli

        base = f"{ROOT}/workspaces/{w}"
        project = db.execute("SELECT project_id FROM source WHERE id=%s", (src,)).fetchone()[0]
        definition = create(web, f"{base}/projects/{project}/aspects", A)
        db.execute("UPDATE browser_admission SET decision_allowed=true WHERE source_id=%s", (src,))
        decision_url = f"{base}/snapshots/{current}/decisions"
        run = post(web, decision_url, operation(src, profile=PROFILE, **definition), 202)["id"]
        assert cli("topic-selection") == "succeeded"
        selected = submit(web, route, operation(src, decision_run=run))["id"]
        selected_rows = db.execute(
            "SELECT v.text_content,t.state FROM topic_input t JOIN review_version v ON (v.workspace_id,v.source_id,v.id)=(%s,%s,t.version_id) WHERE t.result_id=%s",
            (w, src, selected),
        ).fetchall()
        print(
            "TOPIC_REAL_CLASSIFIER",
            sorted(state for text, state in selected_rows if gold[text] == "queue"),
        )
        assert any(
            gold[text] == "queue" and state == "machine_not_mentioned"
            for text, state in selected_rows
        )
        later = dated_snapshot(db, tmp_path, args, w, src, "05", ["Крышка стакана намокла."])
        assert check(later)["receipt"]["state"] == "HISTORY_NOT_COMPARABLE"
        partial = dated_snapshot(
            db, tmp_path, args, w, src, "01", ["Промокла крышка стакана."], "partial"
        )
        assert check(partial)["receipt"]["state"] == "HISTORY_PARTIAL"
        db.execute(
            "UPDATE browser_admission SET topic_discovery_allowed=false WHERE checksum=(SELECT b.file_sha256 FROM import_batch b JOIN snapshot s ON b.id=s.batch_id WHERE s.id=%s)",
            (hist2,),
        )
        assert (
            web.get(
                url, params=q | {"candidate_id": cand["id"], "historical_snapshot": hist2}
            ).status_code
            == 404
        )


def test_topic_discovery_actions_and_selection(db, tmp_path, key, monkeypatch):  # noqa: F811
    texts = (
        ["🍵\r\nКрышка стакана " + t for t in ("намокла", "теряется", "треснула")]
        + ["Звонок двери " + t for t in ("сорван", "сломан", "заклинило")]
        + ["   КРЫШКА  СТАКАНА намокла  ", " "]
    )
    texts[-2] = texts[0].upper() + "   "
    with oracle(
        db,
        tmp_path,
        key,
        monkeypatch,
        inputs=[{"id": str(i), "text": t} for i, t in enumerate(texts)],
    ) as (scope, web, droute, ids, _, project, root):
        w, src, s, aspect, av = scope
        route = droute.split("/decisions/")[0] + "/topic-discovery"
        q = {"source_id": str(src)}
        assert web.get(route, params=q).status_code == 404
        db.execute(
            "UPDATE browser_admission SET topic_discovery_allowed=true WHERE source_id=%s", (src,)
        )
        from operational.tests.test_access import COOKIE
        from operational.tests.test_decision_storage import runtime
        from reviewscope.access.security import session_proof

        with runtime(w, session_proof(web.cookies[COOKIE], key)) as client:
            cap = client.execute(
                "SELECT topic_catalog(%s,%s,%s,NULL,NULL)", (w, src, s)
            ).fetchone()[0]
        assert cap["source_rows_sha256"]
        assert web.get(route, params=q).status_code == 200
        start = operation(src)
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda _: web.post(route, headers=CSRF, json=start), range(2)))
        assert sorted(r.status_code for r in replies) == [200, 201], [r.text for r in replies]
        res = replies[0].json()["id"]
        url = route + "/" + res

        def page(cid=None):
            reply = web.get(url, params=q | ({"candidate_id": cid} if cid else {}))
            assert reply.status_code == 200, reply.text
            return reply.json()

        base = page()
        m = base["manifest"]
        assert [
            m[k] for k in ("N_accepted", "N_blank", "N_unique_inputs", "N_clustered", "N_noise")
        ] == [8, 1, 6, 7, 0]
        parents = [p["id"] for p in base["rows"]]
        assert len(parents) == 2
        group = page(parents[0])

        def change(act, parents, **kwargs):
            return operation(
                src, reason="Проверены исходные версии", action=act, expected_open=parents, **kwargs
            )

        merged = change("merge", parents)
        ev = submit(web, url + "/actions", merged)["id"]
        assert submit(web, url + "/actions", merged, 200)["id"] == ev
        assert (
            web.post(
                url + "/actions", headers=CSRF, json=merged | {"reason": "Другая причина"}
            ).status_code
            == 409
        )
        child = page(parents[0])["children"][0]
        combined = page(child)
        members = [x["version_id"] for x in combined["rows"]]
        assert len(members) == len(set(members)) == 7 and combined["support"] == 6
        assert page(parents[0])["rows"] == group["rows"]
        submit(web, url + "/actions", change("split", [child], parts=[members[:1], members]), 422)
        split = change("split", [child], parts=[members[:1], members[1:]])
        submit(web, url + "/actions", split)
        children = page(child)["children"]
        assert sorted(page(x)["total"] for x in children) == [1, 6]
        a, b = children
        with ThreadPoolExecutor(2) as pool:
            choices = [
                change("reject", [a]),
                change("confirm", [a], aspect_id=str(aspect), aspect_version_id=str(av)),
            ]
            replies = list(
                pool.map(
                    lambda payload: web.post(url + "/actions", headers=CSRF, json=payload), choices
                )
            )
        assert sorted(r.status_code for r in replies) == [201, 409]
        definition = {
            "name": "Протекающий стакан",
            "definition": "Разлив через крышку",
            "include": ["Протечка по краю"],
            "exclude": ["Брызги из крана"],
        }
        confirm = change("confirm", [b], definition=definition)
        submit(web, url + "/actions", confirm)
        submit(web, url + "/actions", confirm, 200)
        record = next(e for e in page(b)["events"] if e["action"] == "confirm")
        assert record["aspect_version_id"] != str(av) and record["aspect_sha"]
        assert submit(web, route, operation(src))["id"] == res
        assert page(b)["events"][-1]["id"] == record["id"]
        missing_history = dated_snapshot(
            db,
            tmp_path,
            [
                "--workspace",
                str(w),
                "--source",
                str(src),
                "--object-root",
                str(root),
                "--coverage-passport",
                str(tmp_path / "coverage"),
            ],
            w,
            src,
            "02",
            ["Звонок двери сломался."],
        )
        submit(
            web,
            url + "/history-checks",
            operation(src, candidate_id=a, historical_snapshot=missing_history),
        )
        unknown = web.get(
            url, params=q | {"candidate_id": a, "historical_snapshot": missing_history}
        ).json()["history"][0]
        assert (
            unknown["receipt"]["state"] == "HISTORY_NOT_AVAILABLE"
            and unknown["receipt"]["N_searched"] == 0
        )
        run = droute.split("/")[-1]
        selected = submit(web, route, operation(src, decision_run=run))["id"]
        sm = web.get(route + "/" + selected, params=q).json()["manifest"]
        assert (
            sm["N_selected"] < m["N_selected"] and sm["artifact_sha256"] and sm["definition_sha256"]
        )
        grant(db, src, True)
        submit(
            web, droute + "/human/reviews/" + ids[-1] + "/events", annotation(src, "dispute", None)
        )
        rev = submit(web, droute + "/human/results", operation(src))["id"]
        result = submit(web, route, operation(src, decision_run=run, human_revision=rev))["id"]
        frozen = web.get(route + "/" + result, params=q).json()["manifest"]
        assert frozen["selection_counts"]["disputed"] == 1 and frozen["human_digest"]
        grant(db, src, False)
        assert web.get(route + "/" + result, params=q).status_code == 404
        for flag in ["candidate_allowed", "evidence_allowed", "topic_discovery_allowed"]:
            db.execute(f"UPDATE browser_admission SET {flag}=false WHERE source_id=%s", (src,))
            assert web.get(url, params=q).status_code == 404
            db.execute(f"UPDATE browser_admission SET {flag}=true WHERE source_id=%s", (src,))
        assert db.execute("SELECT topic_intact(%s,%s,%s)", (w, s, res)).fetchone() == (True,)
        obj = root / str(w) / "sha256" / m["snapshot_object_sha256"]
        obj.chmod(0o600)
        obj.write_bytes(b"corrupt synthetic object")
        assert web.get(url, params=q).status_code == 503


def probe_topic_transaction(w, src, s, proof, pipe):
    from operational.tests.test_decision_storage import runtime

    with runtime(w, proof) as client:
        client.execute("BEGIN")
        staged = client.execute(
            "SELECT topic_publish(%s,%s,%s,NULL,NULL,%s)", (w, src, s, uuid4())
        ).fetchone()[0]
        pipe.send(staged)
        pipe.recv()


def test_topic_discovery_integrity_and_identity(db, tmp_path, key, monkeypatch):  # noqa: F811
    import multiprocessing
    import signal

    from operational.tests.test_decision_storage import cabinet, runtime

    docs = [
        {
            "id": str(i),
            "text": ("😀е́\r\n" + str(i) + " ") * 300 + "Не протекает крышка стакана " + suffix,
        }
        for i, suffix in enumerate(["дома", "днём", "зимой"])
    ]
    with (
        cabinet(db, tmp_path, key, inputs=docs) as (sc, reader, proof, _, web, _),
        importer(tmp_path, monkeypatch),
    ):
        w, src, s = sc[:3]
        db.execute(
            "UPDATE browser_admission SET topic_discovery_allowed=true WHERE source_id=%s", (src,)
        )
        route = f"{ROOT}/workspaces/{w}/snapshots/{s}/topic-discovery"
        ctx = multiprocessing.get_context("spawn")
        receive, send = ctx.Pipe()
        child = ctx.Process(target=probe_topic_transaction, args=(w, src, s, proof, send))
        child.start()
        send.close()
        try:
            assert receive.poll(15) and receive.recv()["code"] == 201
            assert db.execute("SELECT count(*) FROM topic_input").fetchone() == (0,)
        finally:
            child.kill()
            child.join(10)
            receive.close()
        assert child.exitcode == -signal.SIGKILL
        db.execute(
            "CREATE FUNCTION skip_topic_input() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$"
        )
        db.execute(
            "CREATE TRIGGER omit_topic BEFORE INSERT ON topic_input FOR EACH ROW EXECUTE FUNCTION skip_topic_input()"
        )
        submit(web, route, operation(src), 503)
        assert db.execute("SELECT count(*) FROM topic_result").fetchone() == (0,)
        db.execute("DROP TRIGGER omit_topic ON topic_input")
        rid = submit(web, route, operation(src))["id"]
        url = route + "/" + rid
        q = {"source_id": str(src)}
        candidate = web.get(url, params=q).json()["rows"][0]
        cid = candidate["id"]
        data = web.get(url, params=q | {"candidate_id": cid}).json()
        assert data["term"] in ("крышка стакана", "протекает крышка")
        assert len(data["rows"]) == 3
        for member in data["rows"]:
            text = web.get(
                url, params=q | {"candidate_id": cid, "version": member["version_id"]}
            ).json()["evidence"]["text"]
            assert "Не протекает" in text[member["start"] : member["end"]] and member["end"] == len(
                text
            )
        # Relabel the observed cluster and reverse its members without changing the key.
        from psycopg.types.json import Jsonb

        binding = db.execute(
            "SELECT binding FROM topic_result WHERE result_id=%s", (rid,)
        ).fetchone()[0]
        uuids = [row["version_id"] for row in data["rows"]]

        def content_key(scope, ids):
            return db.execute("SELECT topic_key(%s,%s::uuid[])", (Jsonb(scope), ids)).fetchone()[0]

        for labeled in [{7: uuids}, {42: list(reversed(uuids))}]:
            assert [content_key(binding, group) for group in labeled.values()] == [
                candidate["content_key"]
            ]
        assert content_key(binding, uuids[:-1]) != candidate["content_key"]
        assert content_key(binding | {"profile": "other"}, uuids) != candidate["content_key"]
        with runtime(w, proof) as writer:
            writer.execute("BEGIN")
            assert (
                writer.execute(
                    "SELECT topic_review(%s,%s,%s,%s,%s,'reject',%s::uuid[],'Свидетельство просмотрено',NULL,NULL,NULL,NULL)",
                    (w, src, s, rid, uuid4(), [cid]),
                ).fetchone()[0]["code"]
                == 201
            )
            with pytest.raises(psycopg.errors.LockNotAvailable), db.transaction():
                db.execute("SET LOCAL lock_timeout='100ms'")
                db.execute(
                    "UPDATE browser_admission SET topic_discovery_allowed=false WHERE source_id=%s",
                    (src,),
                )
            writer.execute("ROLLBACK")
        assert web.get(url, params=q | {"candidate_id": cid}).json()["events"] == []
        for role in ("rs_runtime", "rs_worker", "rs_upload"):
            assert not db.execute(
                "SELECT has_any_column_privilege(%s,'topic_input','SELECT,INSERT,UPDATE') OR has_function_privilege(%s,'topic_projection(uuid,uuid,uuid,uuid,uuid)','EXECUTE')",
                (role, role),
            ).fetchone()[0]
        print("TOPIC_IDENTITY", candidate["content_key"])
