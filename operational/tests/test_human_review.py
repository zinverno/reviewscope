"""Human-only authoring after a sealed synthetic baseline, through the cabinet's API."""

import hashlib
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from operational import decision_worker, worker
from operational.tests.test_access import CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_browser_delivery import importer
from operational.tests.test_decision_storage import cabinet, enqueue, publish, scores, stage
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision

LABELS = ["complaint", "mixed", "denial", "unclear", *(["not_mentioned"] * 4)]


def operation(src, **fields):
    return {"source_id": str(src), "idempotency_key": str(uuid4()), **fields}


def annotation(src, action, outcome, expected=None, **fields):
    return operation(
        src,
        action=action,
        outcome=outcome,
        expected_event_id=expected,
        reason="Проверен исходный текст",
        selection="all",
        spans=[{"start": 0, "end": 1}] if outcome in ("complaint", "mixed") else [],
        **fields,
    )


def submit(client, url, data, status=201):
    response = client.post(url, headers=CSRF, json=data)
    assert response.status_code == status, (response.status_code, response.text)
    return response.json()


@contextmanager
def oracle(db, tmp_path, signing_key, monkeypatch, *, inputs=None, definition=None):
    if inputs is None:
        rows = [{"id": str(i), "text": f"🍒e\u0301\r\nОжидание {min(i, 6)}"} for i in range(8)]
        inputs = [*rows, {"id": "blank", "text": " "}, rows[0], False, 42]
    with (
        cabinet(db, tmp_path, signing_key, inputs=inputs, **({"definition": definition} if definition else {})) as (s, reader, _, root, web, project),
        worker.connect() as conn,
        importer(tmp_path, monkeypatch),
    ):
        run = enqueue(reader, s)[1]
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()[:3]
        assigned = scores(db, s)
        ids = []
        for row in assigned:
            if row["outcome"] == "excluded_blank":
                continue
            label = LABELS[len(ids) % 8]
            ids.append(row["version_id"])
            show = label != "not_mentioned"
            row.update(
                outcome=label,
                start=0 if show else None,
                end=1 if show else None,
                reason="MODEL_CONTEXT" if show else "ALL_WINDOWS_OFF_TOPIC",
            )
        assert stage(conn, claim, assigned) == len(assigned)
        doc = publish(conn, claim, len(ids), root)

        def forbid_model(*args, **kwargs):
            pytest.fail("saving a human annotation invoked the decision model")

        monkeypatch.setattr(decision_worker, "score_batches", forbid_model)
        route = f"{ROOT}/workspaces/{s[0]}/snapshots/{s[2]}/decisions/{run}"
        yield s, web, route, ids, doc, project, root


def grant(db, source, allowed):
    db.execute(
        "UPDATE browser_admission SET human_review_allowed=%s WHERE source_id=%s", (allowed, source)
    )


def test_reviewed_result_oracle_and_cas(db, tmp_path, monkeypatch, key):  # noqa: F811
    with oracle(db, tmp_path, key, monkeypatch) as (s, web, route, ids, doc, project, root):
        w, src, _, aspect, av = s
        q = dict(source_id=str(src))
        human = route + "/human"
        baseline = web.get(route, params=q)
        assert baseline.status_code == 200
        assert [
            doc["summary"][k]
            for k in (
                "N_input",
                "N_accepted",
                "N_quarantine",
                "N_confirmed_duplicate",
                "N_excluded",
                "N_eligible",
                "N_mentioned",
                "N_complaints",
                "N_uncertain",
            )
        ] == [12, 9, 2, 1, 1, 8, 4, 2, 1]
        assert web.get(human + "/queue", params=q).status_code == 404
        grant(db, src, True)

        def read(path, **params):
            res = web.get(human + path, params=q | params)
            assert res.status_code == 200, (res.status_code, res.text)
            return res.json()

        def events(i):
            return f"{human}/reviews/{ids[i]}/events"

        def freeze():
            return submit(web, human + "/results", operation(src))["id"]

        def report(id):
            return read("/results/" + id)

        assert read("/queue", mode="uncertain")["rows"][0]["version_id"] == ids[3]
        assert read("/queue", mode="disagreement")["total"] == 0
        assert read("/queue", outcome="not_mentioned", state="unreviewed")["total"] == 4
        plan_args = operation(src, seed=str(uuid4()), size=8)
        plan = submit(web, human + "/plans", plan_args)["id"]
        assert submit(web, human + "/plans", plan_args, 200)["id"] == plan
        all_sample = read("/queue", mode="sample", plan_id=plan)
        assert {r["version_id"] for r in all_sample["rows"]} == set(ids)
        assert read("/plans")["items"][0]["size"] == 8
        assert read("/queue", mode="sample", plan_id=plan, offset=25)["rows"] == []
        empty = report(freeze())
        assert empty["summary"]["complaints_among_resolved"] is None
        password = secrets.token_urlsafe(24)
        provision(db, "reviewer-two", password, key, workspace=w, role="analyst")
        with TestClient(create_app(), base_url=ORIGIN) as peer:
            assert login(peer, "reviewer-two", password).status_code == 204
            first = annotation(src, "correct", "denial")
            other = annotation(src, "correct", "positive")
            with ThreadPoolExecutor(2) as pool:
                a = pool.submit(web.post, events(0), json=first, headers=CSRF)
                b = pool.submit(peer.post, events(0), json=other, headers=CSRF)
                ca, cb = a.result(), b.result()
            assert sorted([ca.status_code, cb.status_code]) == [201, 409]
            if cb.status_code == 201:
                first["expected_event_id"] = cb.json()["id"]
                fp = submit(web, events(0), first)["id"]
            else:
                fp = ca.json()["id"]
            assert submit(web, events(0), first, 200)["id"] == fp
            submit(peer, events(0), first, 409)
            submit(web, events(0), first | {"reason": "Другая причина"}, 409)
            submit(web, events(2), first, 409)
            trial = annotation(src, "correct", "complaint") | dict(
                selection="sample",
                plan_id=plan,
                spans=[{"start": 1, "end": 3}, {"start": 5, "end": 8}],
            )
            with ThreadPoolExecutor(2) as pool:
                replies = list(
                    pool.map(lambda _: web.post(events(4), json=trial, headers=CSRF), range(2))
                )
            assert sorted(r.status_code for r in replies) == [200, 201]
            fn = replies[0].json()["id"]
            assert replies[1].json()["id"] == fn
        confirmed = submit(web, events(1), annotation(src, "confirm", "mixed"))["id"]
        disputed = submit(web, events(3), annotation(src, "dispute", None))["id"]
        dis = read("/queue", mode="disagreement")
        assert dis["rows"][0]["event_id"] == disputed and dis["total"] == 3
        old_id = freeze()
        old = report(old_id)
        counts = old["summary"]
        assert [
            counts[k]
            for k in (
                "eligible",
                "resolved",
                "disputed",
                "unreviewed",
                "confirmed",
                "corrected",
                "complaints",
                "reviewed",
            )
        ] == [8, 3, 1, 4, 1, 2, 2, 4]
        assert counts["complaints_among_resolved"] == pytest.approx(2 / 3)
        assert {r["event_id"] for r in old["rows"] if r["event_id"]} == {
            fp,
            fn,
            confirmed,
            disputed,
        }
        assert {r["version_id"] for r in old["rows"] if r["outcome"] in ("complaint", "mixed")} == {
            ids[4],
            ids[1],
        }
        submit(web, events(5), annotation(src, "correct", "complaint"))
        new_id = freeze()
        new = report(new_id)
        assert old_id != new_id and old["digest"] != new["digest"]
        assert new["summary"]["complaints"] == 3 and new["summary"]["resolved"] == 4
        assert report(old_id) == old
        assert {r["version_id"] for r in new["rows"] if r["outcome"] in ("complaint", "mixed")} == {
            ids[4],
            ids[1],
            ids[5],
        }
        hist = read(f"/reviews/{ids[4]}/history")
        assert hist["head"] == fn and hist["events"][0]["spans"] == trial["spans"]
        assert read("/queue", mode="sample", plan_id=plan)["total"] == 8
        event = annotation(src, "confirm", "unclear", expected=disputed)
        submit(web, events(3), event)
        assert read(f"/reviews/{ids[3]}/history")["total"] == 2
        submit(
            web,
            events(6),
            annotation(src, "correct", "complaint") | {"spans": [{"start": 0, "end": 10000}]},
            422,
        )
        submit(web, events(6), annotation(src, "correct", "complaint", expected=fp), 409)
        blank = next(r for r in baseline.json()["rows"] if r["outcome"] == "excluded_blank")
        assert web.post(
            f"{human}/reviews/{blank['version_id']}/events",
            headers=CSRF,
            json=annotation(src, "correct", "complaint"),
        ).status_code in (404, 422)
        assert web.get(route, params=q).content == baseline.content
        # Archiving an aspect must preserve the exact baseline and the frozen revision.
        archived = web.post(
            f"{ROOT}/workspaces/{w}/projects/{project}/aspects/{aspect}/archive",
            headers=CSRF,
            json=dict(expected_version=str(av), idempotency_key=str(uuid4())),
        )
        assert archived.status_code == 200
        assert report(old_id) == old
        after_archive = baseline.json()
        after_archive["definition"]["archived"] = True
        assert web.get(route, params=q).json() == after_archive
        grant(db, src, False)
        assert web.get(human + "/results/" + old_id, params=q).status_code == 404
        assert web.post(events(4), headers=CSRF, json=trial).status_code == 404
        grant(db, src, True)
        artifact = baseline.json()["artifact_sha256"]
        assert artifact == old["artifact_sha256"] and old["rows_sha256"] == doc["rows_sha256"]
        obj = root / str(w) / "sha256" / artifact
        before = obj.read_bytes()
        assert hashlib.sha256(before).hexdigest() == artifact
        obj.rename(obj.with_suffix(".held"))
        assert web.get(human + "/results/" + old_id, params=q).status_code in (404, 503)
        assert web.post(
            events(7), headers=CSRF, json=annotation(src, "correct", "complaint")
        ).status_code in (404, 503)
        obj.with_suffix(".held").rename(obj)
        assert read(f"/reviews/{ids[7]}/history")["total"] == 0


@pytest.mark.parametrize("blank", [False, True])
def test_reviewed_result_empty_and_disputed(db, tmp_path, monkeypatch, key, blank):  # noqa: F811
    inputs = [{"id": "one", "text": " " if blank else "Первая версия"}]
    with oracle(db, tmp_path, key, monkeypatch, inputs=inputs) as (s, web, route, ids, doc, _, _):
        src = s[1]
        q = dict(source_id=str(src))
        human = route + "/human"
        grant(db, src, True)
        plan = submit(web, human + "/plans", operation(src, seed=str(uuid4()), size=25))["id"]
        sampled = web.get(human + "/queue", params=q | dict(mode="sample", plan_id=plan)).json()
        assert sampled["total"] == sampled["eligible"] == len(ids) == int(not blank)

        def freeze():
            id = submit(web, human + "/results", operation(src))["id"]
            return web.get(human + "/results/" + id, params=q).json()["summary"]

        if blank:
            counts = freeze()
            assert (
                counts["eligible"] == counts["reviewed"] == 0
                and counts["complaints_among_resolved"] is None
            )
            return
        events = human + f"/reviews/{ids[0]}/events"
        disputed = submit(web, events, annotation(src, "dispute", None))["id"]
        counts = freeze()
        assert (
            counts["reviewed"] == counts["disputed"] == 1
            and counts["complaints_among_resolved"] is None
        )
        submit(web, events, annotation(src, "correct", "not_mentioned", expected=disputed))
        counts = freeze()
        assert (
            counts["reviewed"] == counts["resolved"] == 1
            and counts["complaints_among_resolved"] == 0
        )


def test_review_publication_consistent_and_scoped(db, tmp_path, monkeypatch, key):  # noqa: F811
    from operational.tests.test_access import COOKIE
    from operational.tests.test_decision_storage import runtime
    from reviewscope.access.ledger import ledger_path
    from reviewscope.access.security import session_proof

    with oracle(db, tmp_path, key, monkeypatch) as (s, web, route, ids, doc, _, _):
        src = s[1]
        q = dict(source_id=str(src))
        human = route + "/human"
        grant(db, src, True)
        run = route.rsplit("/", 1)[-1]
        scope = (*s[:3], run)
        actor_proof = session_proof(web.cookies[COOKIE], key)
        with runtime(s[0], actor_proof) as reader:

            def plan(w, source, snapshot, r):
                return reader.execute(
                    "SELECT human_queue(%s,%s,%s,%s,'all',NULL,NULL,NULL,0)",
                    (w, source, snapshot, r),
                ).fetchone()[0]

            assert plan(*scope)["total"] == 8
            for i in range(4):
                foreign = list(scope)
                foreign[i] = uuid4()
                assert plan(*foreign) is None
            with reader.transaction(force_rollback=True):
                ok = reader.execute(
                    "SELECT human_publish(%s,%s,%s,%s,%s)", (*scope, uuid4())
                ).fetchone()[0]
                assert ok["code"] == 201
            assert web.get(human + "/results", params=q).json()["total"] == 0
            for flag in ("candidate_allowed", "evidence_allowed", "decision_allowed"):
                db.execute(f"UPDATE browser_admission SET {flag}=false WHERE source_id=%s", (src,))
                assert plan(*scope) is None
                db.execute(f"UPDATE browser_admission SET {flag}=true WHERE source_id=%s", (src,))
        url = f"{human}/reviews/{ids[4]}/events"
        with ThreadPoolExecutor(2) as pool:
            a = pool.submit(submit, web, url, annotation(src, "correct", "complaint"))
            b = pool.submit(submit, web, human + "/results", operation(src))
            event, published = a.result()["id"], b.result()["id"]
        exact = human + "/results/" + published
        frozen = web.get(exact, params=q)
        item = next(r for r in frozen.json()["rows"] if r["version_id"] == ids[4])
        assert item["event_id"] in (None, event)
        assert frozen.json()["summary"]["complaints"] == int(item["event_id"] == event)
        assert web.get(exact, params=q).content == frozen.content
        assert (
            web.post(
                url,
                headers=CSRF | {"Content-Type": "application/json"},
                content=b'{"reason":"\xff"}',
            ).status_code
            == 422
        )
        assert (
            web.post(
                url,
                headers=CSRF | {"Content-Type": "application/json"},
                content=b'{"reason":"x","reason":"y"}',
            ).status_code
            == 422
        )
        witness = ledger_path()
        witness.rename(witness.with_suffix(".held"))
        assert web.get(exact, params=q).status_code == 503
        witness.with_suffix(".held").rename(witness)
        who = db.execute("SELECT id FROM principal WHERE login='annotator'").fetchone()[0]
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (s[0], who),
        )
        assert web.get(exact, params=q).status_code == 403
        db.execute(
            "UPDATE workspace_membership SET role='owner' WHERE workspace_id=%s AND principal_id=%s",
            (s[0], who),
        )
        db.execute(
            "UPDATE access_session SET revoked_at=clock_timestamp() WHERE principal_id=%s", (who,)
        )
        assert web.get(exact, params=q).status_code == 401
