"""Trace canonical evidence to immutable positions under actual session, SQL and restore gates."""

import hashlib
import logging
import secrets
from uuid import uuid4

import psycopg
from fastapi.testclient import TestClient

from operational.retention import capture_access
from operational.tests.test_access import ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_deliveries import FIX, call, command, csv, json_input
from operational.tests.test_schema import scope
from reviewscope.access.http import create_app
from reviewscope.access.operator import provision


def test_evidence_scope_history_and_filters(db, tmp_path, monkeypatch, key, caplog):  # noqa: F811
    caplog.set_level(logging.INFO, logger="reviewscope.access.requests")
    args, _, w, s = command(db, tmp_path)
    other, _, _ = scope(db, source_key="synthetic")
    secret = secrets.token_urlsafe(24)
    owner = provision(db, "reader", secret, key, workspace=w)
    provision(db, "other-reader", secret, key, workspace=other)
    a = call(args + csv("a") + ["--apply"])["snapshot_id"]
    b = call(args + csv("b") + ["--apply"])["snapshot_id"]
    base = f"{ROOT}/workspaces/{w}/snapshots"
    left, right = f"{base}/{a}/reviews", f"{base}/{b}/reviews"
    with TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "reader", secret).status_code == 204
        for name in ("a.csv", "b.csv"):
            db.execute(
                "INSERT INTO browser_admission VALUES(%s,%s,%s)",
                (w, s, hashlib.sha256((FIX / name).read_bytes()).hexdigest()),
            )
        assert web.get(left).status_code == 404
        closed = capture_access(db)
        db.execute("UPDATE browser_admission SET evidence_allowed=true WHERE workspace_id=%s", (w,))
        assert capture_access(db) != closed
        r = web.get(left)
        assert r.status_code == 200, r.status_code
        first = r.json()
        second = web.get(right).json()
        assert first["total"] == 3 and second["total"] == 6
        assert first["historical"] and not second["historical"]
        assert first["coverage"] == "unknown" and first["covered_from"] is None
        old, new = first["rows"], second["rows"]
        assert [v["position"] for v in new] == list(range(1, 7))
        assert [v["version_id"] for v in old[1:]] == [v["version_id"] for v in new[1:3]]
        assert (
            old[0]["identity_id"] == new[0]["identity_id"]
            and old[0]["version_id"] != new[0]["version_id"]
        )
        assert (
            old[0]["version_no"] == 1 and new[0]["version_no"] == 2 and new[4]["review_id"] is None
        )
        assert all(v["full_text"] is None for v in new)
        detail_path = left + "/" + old[0]["version_id"]
        detail = web.get(detail_path).json()["rows"][0]
        assert detail["full_text"].startswith("На табло") and detail["utc_offset"] == "+03:00"
        assert (
            detail["version_batch_id"] == first["batch_id"]
            and new[1]["version_batch_id"] == first["batch_id"]
        )
        assert detail["evidence_span"] is detail["source_url"] is None
        db.execute(
            "UPDATE browser_admission SET evidence_allowed=false WHERE workspace_id=%s AND checksum=%s",
            (w, hashlib.sha256((FIX / "a.csv").read_bytes()).hexdigest()),
        )
        assert web.get(detail_path).status_code == 404 and web.get(right).status_code == 200
        db.execute("UPDATE browser_admission SET evidence_allowed=true WHERE workspace_id=%s", (w,))
        assert web.get(right + "/" + old[0]["version_id"]).status_code == 404
        assert (
            web.get(
                right,
                params={
                    "position": 5,
                    "rating": 3,
                    "source_id": s,
                    "location_id": new[4]["location_id"],
                    "has_text": True,
                    "has_date": True,
                    "first_day": "2026-10-10",
                    "last_day": "2026-10-10",
                },
            ).json()["total"]
            == 1
        )
        assert web.get(right, params={"offset": 2, "limit": 2}).json()["rows"] == new[2:4]
        assert web.get(right, params={"offset": 9}).json()["total"] == 6
        assert web.get(right, params={"source_id": uuid4()}).json()["rows"] == []
        assert (
            web.get(right + "?limit=51").status_code
            == web.get(right + "?rating=1&rating=2").status_code
            == 422
        )

        j = tmp_path / "long"
        j.mkdir()
        marker = "<b>Вымышленный текст</b> " * 150
        inp = json_input(
            j,
            [
                {"id": "long", "body": marker, "url": "http://example.test/unreleased"},
                {"id": "conflict", "body": "один"},
                {"id": "conflict", "body": "два"},
            ],
            {"id": "source_review_id", "body": "text", "url": "source_url"},
        )
        snap = call(args + inp + ["--apply"])["snapshot_id"]
        db.execute(
            "INSERT INTO browser_admission VALUES(%s,%s,%s,true,ARRAY[%s]::sha256_hex[])",
            (
                w,
                s,
                hashlib.sha256((j / "data").read_bytes()).hexdigest(),
                hashlib.sha256(b"http://example.test/unreleased").hexdigest(),
            ),
        )
        path = f"{base}/{snap}/reviews"
        trace = web.get(path).json()
        assert trace["total"] == 1 and len(trace["rows"][0]["excerpt"]) == 160
        assert (
            web.get(path + "/" + trace["rows"][0]["version_id"]).json()["rows"][0]["full_text"]
            == marker
        )
        assert (
            web.get(path + "/" + trace["rows"][0]["version_id"]).json()["rows"][0]["source_url"]
            is None
        )
        assert web.get(path, params={"position": 2}).json()["rows"] == []
        assert marker[:24] not in caplog.text
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE principal_id=%s AND workspace_id=%s",
            (owner, w),
        )
        assert web.get(detail_path).status_code == 403
        db.execute(
            "UPDATE workspace_membership SET state='inactive' WHERE principal_id=%s AND workspace_id=%s",
            (owner, w),
        )
        assert web.get(detail_path).status_code == 404
        assert login(web, "other-reader", secret).status_code == 204
        absent = web.get(f"{base}/{uuid4()}/reviews")
        forbidden = web.get(detail_path)
        assert (
            absent.status_code == forbidden.status_code == 404 and absent.json() == forbidden.json()
        )
    with psycopg.connect(user="rs_runtime") as role:
        assert role.execute(
            "SELECT has_any_column_privilege(current_user,'review_version','SELECT'),has_any_column_privilege(current_user,'import_row','SELECT')"
        ).fetchone() == (False, False)
        assert role.execute(
            "SELECT review_evidence_page(%s,%s,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,5)",
            (w, a),
        ).fetchone() == (None,)
