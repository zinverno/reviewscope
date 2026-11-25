"""Human labels belong to the exact successful run, definition and accepted review version."""

import json
from importlib.resources import files
from threading import Event
from uuid import uuid4

from operational import worker
from operational.tests.test_access import ROOT, key  # noqa: F401
from operational.tests.test_aspects import B, create, post
from operational.tests.test_browser_delivery import importer
from operational.tests.test_decision_storage import PROFILE, cabinet, query
from operational.tests.test_human_review import annotation, grant, operation, submit

TEXT = "Сорок минут стояли перед кассой. Готовый заказ выдали сразу."


def test_human_definition_run_and_review_version_scope(db, tmp_path, monkeypatch, key):  # noqa: F811
    inputs = [{"id": identity, "text": TEXT} for identity in ("first", "second")]
    with (
        cabinet(db, tmp_path, key, inputs=inputs) as (s, reader, _, _, web, project),
        worker.connect() as conn,
        importer(tmp_path, monkeypatch),
    ):
        w, src, snap, aspect, av = s
        q = {"source_id": str(src)}
        definitions = f"{ROOT}/workspaces/{w}/projects/{project}/aspects"
        base = f"{ROOT}/workspaces/{w}/snapshots/{snap}/decisions"
        grant(db, src, True)

        def baseline(binding):
            status = post(web, base, operation(src, profile=PROFILE, **binding), 202)
            claimed = conn.execute("SELECT * FROM claim_job()").fetchone()
            assert str(claimed[1]) == status["id"]
            assert worker.process(conn, claimed, Event()) == "succeeded"
            url = f"{base}/{status['id']}"
            response = web.get(url, params=q)
            assert response.status_code == 200
            machine = response.json()
            assert machine["definition"]["version_id"] == binding["version_id"]
            assert machine["summary"]["N_eligible"] == 2
            return url, status["id"], machine

        def history(url, version):
            response = web.get(f"{url}/human/reviews/{version}/history", params=q)
            assert response.status_code == 200
            return response.json()

        def label(url, version, outcome):
            prior = history(url, version)
            assert prior["head"] is None
            action = "confirm" if prior["machine"] == outcome else "correct"
            return submit(
                web, f"{url}/human/reviews/{version}/events", annotation(src, action, outcome)
            )["id"]

        def publish(url):
            rev = submit(web, url + "/human/results", operation(src))["id"]
            response = web.get(f"{url}/human/results/{rev}", params=q)
            assert response.status_code == 200
            return rev, response.content

        a = {"aspect_id": str(aspect), "version_id": str(av)}
        b = create(web, definitions, B)
        original, original_run, machine = baseline(a)
        first, twin = machine["rows"]
        assert first["text_sha256"] == twin["text_sha256"]
        assert first["content_sha256"] == twin["content_sha256"]
        first, twin = first["version_id"], twin["version_id"]
        assert first != twin
        complaint = label(original, first, "complaint")
        assert history(original, twin)["total"] == 0
        revision, frozen = publish(original)
        assert json.loads(frozen)["summary"]["complaints"] == 1
        assert {row["version_id"] for row in json.loads(frozen)["rows"] if row["event_id"]} == {
            first
        }

        revised_payload = json.loads(
            files("reviewscope.resources").joinpath("decision-definitions.json").read_bytes()
        )["charge"]
        revised = post(
            web,
            f"{definitions}/{aspect}/versions",
            {
                "expected_version": str(av),
                "payload": revised_payload,
                "idempotency_key": str(uuid4()),
            },
            201,
        )
        assert revised["aspect_id"] == str(aspect) and revised["version_id"] != str(av)

        for binding in (a, b, revised):
            url, run, scoped_machine = baseline(binding)
            assert run != original_run
            assert {row["version_id"] for row in scoped_machine["rows"]} == {first, twin}
            assert history(url, first)["head"] is None
            assert web.get(f"{url}/human/results/{revision}", params=q).status_code == 404
            assert (
                query(reader, "human_result(%s,%s,%s,%s,%s,0)", (w, src, snap, run, revision))
                is None
            )
            submit(
                web,
                f"{url}/human/reviews/{first}/events",
                annotation(src, "dispute", None, expected=complaint),
                409,
            )
            independent = label(url, first, "denial")
            assert independent != complaint and history(url, first)["head"] == independent
            _, report = publish(url)
            assert json.loads(report)["summary"]["complaints"] == 0

        assert history(original, first)["head"] == complaint
        assert history(original, twin)["total"] == 0
        assert web.get(f"{original}/human/results/{revision}", params=q).content == frozen
        assert web.get(original, params=q).json()["rows"] == machine["rows"]


def test_unfinished_failed_decisions_and_expired_session(db, tmp_path, monkeypatch, key):  # noqa: F811
    with (
        cabinet(db, tmp_path, key, inputs=[{"id": "one", "text": TEXT}]) as (
            s,
            reader,
            _,
            _,
            web,
            _,
        ),
        worker.connect() as conn,
        importer(tmp_path, monkeypatch),
    ):
        w, src, snap, aspect, av = s
        q = {"source_id": str(src)}
        base = f"{ROOT}/workspaces/{w}/snapshots/{snap}/decisions"
        selection = dict(profile=PROFILE, aspect_id=str(aspect), version_id=str(av))
        grant(db, src, True)
        run = post(web, base, operation(src, **selection), 202)["id"]
        human = f"{base}/{run}/human"
        version = query(db, "version_id FROM snapshot_member WHERE snapshot_id=%s", (snap,))

        def inaccessible(status):
            assert web.get(human + "/queue", params=q).status_code == status
            submit(
                web, f"{human}/reviews/{version}/events", annotation(src, "dispute", None), status
            )
            submit(web, human + "/results", operation(src), status)
            assert (
                query(reader, "human_history(%s,%s,%s,%s,%s,0)", (w, src, snap, run, version))
                is None
            )
            assert (
                query(reader, "human_publish(%s,%s,%s,%s,%s)", (w, src, snap, run, uuid4())) is None
            )

        inaccessible(404)
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()
        assert str(claim[1]) == run
        inaccessible(404)
        assert (
            query(conn, "worker_step(%s,%s,%s,'summarize',0,1,'MODEL_UNAVAILABLE')", claim[:3])
            == "failed"
        )
        inaccessible(404)
        assert query(db, "count(*) FROM human_review_result WHERE run_id=%s", (run,)) == 0
        assert query(db, "count(*) FROM human_review_event WHERE run_id=%s", (run,)) == 0

        run = post(web, base, operation(src, **selection), 202)["id"]
        human = f"{base}/{run}/human"
        claim = conn.execute("SELECT * FROM claim_job()").fetchone()
        assert str(claim[1]) == run and worker.process(conn, claim, Event()) == "succeeded"
        event = submit(web, f"{human}/reviews/{version}/events", annotation(src, "dispute", None))[
            "id"
        ]
        revision = submit(web, human + "/results", operation(src))["id"]
        assert web.get(f"{human}/results/{revision}", params=q).status_code == 200
        db.execute("""UPDATE access_session SET created_at=now()-interval '2 hours', expires_at=now()-interval '1 minute'
                      WHERE principal_id=(SELECT id FROM principal WHERE login='annotator')""")
        inaccessible(401)
        assert web.get(f"{human}/results/{revision}", params=q).status_code == 401
        assert str(query(db, "id FROM human_review_event WHERE run_id=%s", (run,))) == event
        assert str(query(db, "id FROM human_review_result WHERE run_id=%s", (run,))) == revision
