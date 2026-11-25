"""Exercise published pairs through the private API with authenticated synthetic imports."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from operational.tests.test_access import COOKIE, CSRF, key  # noqa: F401
from operational.tests.test_decision_storage import runtime
from operational.tests.test_human_review import annotation, grant, operation, oracle, submit
from reviewscope.access.security import session_proof


def test_typed_relations_freeze_and_scope(db, tmp_path, key, monkeypatch):  # noqa: F811
    texts = [
        "🛶é\r\nКассира перед заказом ждали час",
        "🛶é\r\nКассира перед заказом ждали час",
        "Кассир перед заказом подошёл сразу",
        "Оформления у терминала долго ждали",
        " ",
    ]
    rows = [dict(id=str(i), text=t) for i, t in enumerate(texts)]
    with oracle(db, tmp_path, key, monkeypatch, inputs=rows) as (s, web, route, ids, _, _, root):
        w, src, sn, _, _ = s
        run = route.split("/")[-1]
        url = route.split("/decisions")[0] + "/relations"
        q = {"source_id": str(src)}
        assert web.get(url, params=q).status_code == 404
        db.execute("UPDATE browser_admission SET relation_allowed=true WHERE source_id=%s", (src,))
        assert web.get(url, params=q).status_code == 200
        args = operation(src)
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda _: web.post(url, headers=CSRF, json=args), range(2)))
        assert sorted(x.status_code for x in replies) == [200, 201]
        res = replies[0].json()["id"]
        assert replies[1].json()["id"] == res

        def page(result, **query):
            response = web.get(url + "/" + result, params=q | query)
            assert response.status_code == 200, response.text
            return response.json()

        base = page(res)
        assert base["manifest"]["eligible_review_count"] == 4
        assert base["manifest"]["excluded_blank_count"] == 1
        assert base["manifest"]["total_possible_pairs"] == 6
        exact = next(p for p in base["pairs"] if p["text_kind"] == "exact_text")
        assert exact["aspect_kind"] == "not_evaluated"
        assert exact["left"]["version_id"] < exact["right"]["version_id"]
        selection = {k: exact[k]["version_id"] for k in ("left", "right")}
        detail = page(res, **selection)["pairs"][0]
        assert detail["left"]["text"] == detail["right"]["text"] == texts[0]
        assert web.get(url + "/" + res, params=q | {"digest": "0" * 64}).status_code == 404
        assert web.get(url + "/" + res, params=q | {"other": "x"}).status_code == 422
        grant(db, src, True)
        human = route + "/human"
        events = {}
        by_text = db.execute(
            "SELECT id,text_content FROM review_version WHERE source_id=%s ORDER BY id", (src,)
        ).fetchall()
        for v, text in by_text:
            if text.strip():
                initial = web.get(f"{human}/reviews/{v}/history", params=q).json()["machine"]
                stance = "denial" if "сразу" in text else "complaint"
                sent = annotation(src, "confirm" if stance == initial else "correct", stance)
                sent["spans"] = [{"start": text.index("К") if "К" in text else 0, "end": len(text)}]
                events[str(v)] = submit(web, f"{human}/reviews/{v}/events", sent)["id"]
        r1 = submit(web, human + "/results", operation(src))["id"]
        inputs = {"decision_run": run, "human_revision": r1}
        assert web.post(url, headers=CSRF, json=args | inputs).status_code == 409
        graph = submit(web, url, operation(src, **inputs))["id"]
        frozen = page(graph)
        assert {p["aspect_kind"] for p in frozen["pairs"]} == {
            "opposite_stance",
            "same_specific_complaint",
        }
        pair = next(p for p in frozen["pairs"] if p["aspect_kind"] == "opposite_stance")
        assert pair["aspect_basis"] == "derived_from_human_outcomes"
        for side in ("left", "right"):
            assert events[pair[side]["version_id"]] == pair[side]["event_id"]
        v = pair["left"]["version_id"]
        submit(web, f"{human}/reviews/{v}/events", annotation(src, "dispute", None, events[v]))
        r2 = submit(web, human + "/results", operation(src))["id"]
        next_result = submit(web, url, operation(src, decision_run=run, human_revision=r2))["id"]
        assert any(p["aspect_kind"] == "uncertain" for p in page(next_result)["pairs"])
        assert page(graph) == frozen
        assert (
            web.post(
                url,
                headers=CSRF,
                json=operation(src, decision_run=run, human_revision=str(uuid4())),
            ).status_code
            == 404
        )
        assert db.execute("SELECT relation_intact(%s,%s,%s)", (w, sn, graph)).fetchone()[0]
        assert web.get(url + "/" + graph, params={"source_id": str(uuid4())}).status_code == 404
        grant(db, src, False)
        assert web.get(url + "/" + graph, params=q).status_code == 404
        assert page(res) == base
        for permission in ("candidate_allowed", "evidence_allowed", "relation_allowed"):
            db.execute(
                f"UPDATE browser_admission SET {permission}=false WHERE source_id=%s", (src,)
            )
            assert web.get(url + "/" + res, params=q).status_code == 404
            db.execute(f"UPDATE browser_admission SET {permission}=true WHERE source_id=%s", (src,))
        # Loss of the producer connection after SQL publication but before commit rolls back both tables.
        abort_key = uuid4()
        conn = runtime(w, session_proof(web.cookies[COOKIE], key))
        try:
            conn.execute("BEGIN")
            staged = conn.execute(
                "SELECT relation_publish(%s,%s,%s,NULL,NULL,%s)", (w, src, sn, abort_key)
            ).fetchone()[0]
            assert staged["code"] == 201
            assert db.execute(
                "SELECT count(*) FROM relation_pair WHERE result_id=%s", (staged["id"],)
            ).fetchone() == (0,)
        finally:
            conn.close()
        retry = submit(web, url, operation(src) | {"idempotency_key": str(abort_key)})
        assert retry["id"] != staged["id"] and page(retry["id"])["digest"] == base["digest"]
        assert page(res, exact_only=True)["filtered"] == 1
        assert page(res, exact_only=True)["manifest"] == base["manifest"]
        object_path = root / str(w) / "sha256" / base["manifest"]["snapshot_object"]
        object_path.chmod(0o600)
        object_path.write_bytes(b"corrupt synthetic snapshot")
        assert web.get(url + "/" + res, params=q).status_code == 503


def test_typed_relations_abstain_on_incomplete_stance(db, tmp_path, key, monkeypatch):  # noqa: F811
    import psycopg
    import pytest

    texts = [
        "Касса у воды. " + t
        for t in (
            "Очередь на час.",
            "Приступили без ожидания.",
            "Очередь то была, то нет.",
            "Вроде долго.",
            "Не знаю, у какого окна.",
            "Красивый прилавок.",
            "Всё хорошо.",
            "Пока не оценено.",
        )
    ]
    with oracle(
        db,
        tmp_path,
        key,
        monkeypatch,
        inputs=[dict(id=str(i), text=t) for i, t in enumerate(texts)],
    ) as (s, web, route, *_):
        w, src, sn = s[:3]
        db.execute(
            "UPDATE browser_admission SET relation_allowed=true,human_review_allowed=true WHERE source_id=%s",
            (src,),
        )
        ids = [
            str(
                db.execute(
                    "SELECT id FROM review_version WHERE source_id=%s AND text_content=%s", (src, t)
                ).fetchone()[0]
            )
            for t in texts
        ]
        q = dict(source_id=str(src))
        h = route + "/human"
        for v, label in zip(
            ids,
            ["complaint", "denial", "mixed", "unclear", "topic_uncertain", "positive"],
            strict=False,
        ):
            machine = web.get(f"{h}/reviews/{v}/history", params=q).json()["machine"]
            submit(
                web,
                f"{h}/reviews/{v}/events",
                annotation(src, "confirm" if machine == label else "correct", label),
            )
        revision = submit(web, h + "/results", operation(src))["id"]
        url = route.split("/decisions")[0] + "/relations"
        result = submit(
            web, url, operation(src, decision_run=route.split("/")[-1], human_revision=revision)
        )["id"]

        def read(a, b):
            left, right = sorted((ids[a], ids[b]))
            return web.get(url + "/" + result, params=q | dict(left=left, right=right)).json()[
                "pairs"
            ][0]

        for i in range(1, 8):
            pair = read(0, i)
            assert pair["aspect_kind"] == "uncertain"
        assert read(6, 7)["reason"] == "unreviewed_endpoint"
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable"):
            db.execute("UPDATE relation_pair SET evidence='{}' WHERE result_id=%s", (result,))
        assert db.execute("SELECT relation_intact(%s,%s,%s)", (w, sn, result)).fetchone()[0]


def test_typed_relations_handover_and_no_transitive_link(db, tmp_path, key, monkeypatch):  # noqa: F811
    from operational.tests.test_aspects import B

    texts = [
        "Упакованную корзину ждали час.",
        "Упакованную коробку передали сразу.",
        "Коробку вручили мгновенно.",
    ]
    with oracle(
        db,
        tmp_path,
        key,
        monkeypatch,
        inputs=[dict(id=str(i), text=t) for i, t in enumerate(texts)],
        definition=B,
    ) as (s, web, route, *_):
        w, src, sn, *_ = s
        db.execute(
            "UPDATE browser_admission SET relation_allowed=true,human_review_allowed=true WHERE source_id=%s",
            (src,),
        )
        url = route.split("/decisions")[0] + "/relations"
        q = dict(source_id=str(src))
        res = submit(web, url, operation(src))["id"]
        page = web.get(url + "/" + res, params=q).json()
        assert page["manifest"]["total_possible_pairs"] == 3 and len(page["pairs"]) == 2
        ids = [
            str(
                db.execute(
                    "SELECT id FROM review_version WHERE source_id=%s AND text_content=%s", (src, t)
                ).fetchone()[0]
            )
            for t in texts
        ]
        left, right = sorted((ids[0], ids[2]))
        assert web.get(url + "/" + res, params=q | dict(left=left, right=right)).status_code == 404
        for v, text, label in zip(ids, texts, ["complaint", "denial", "denial"], strict=True):
            history = route + "/human/reviews/" + v
            machine = web.get(history + "/history", params=q).json()["machine"]
            sent = annotation(src, "confirm" if machine == label else "correct", label)
            sent["spans"] = [dict(start=0, end=len(text))]
            submit(web, history + "/events", sent)
        rev = submit(web, route + "/human/results", operation(src))["id"]
        res2 = submit(
            web, url, operation(src, decision_run=route.split("/")[-1], human_revision=rev)
        )["id"]
        detail = web.get(url + "/" + res2, params=q | dict(left=left, right=right)).json()
        edge = detail["pairs"][0]
        assert edge["channels"] == {"frozen_endpoints": True}
        assert edge["aspect_kind"] == "opposite_stance" and edge["text_kind"] == "not_evaluated"
        assert detail["manifest"]["aspect_version_id"] == s[4]
