"""Synthetic snapshot author context at the API, operator, SQL role and restore boundaries."""

import hashlib
import json
import logging
import secrets
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from operational.retention import capture_access
from operational.tests.test_access import COOKIE, CSRF, ORIGIN, ROOT, key, login  # noqa: F401
from operational.tests.test_deliveries import FIX, call, command, csv, json_input
from operational.tests.test_schema import bind_place, scope
from reviewscope.access.http import create_app
from reviewscope.access.ledger import ledger_path
from reviewscope.access.operator import provision
from reviewscope.access.security import session_proof
from reviewscope.import_preview import preview
from reviewscope.specificity import specificity_score

AUTHOR = "synthetic-context-marker"
BODY = "Вымышленный пример с 15 минутами ожидания"
FIELDS = {
    "id": "source_review_id",
    "author": "reviewer_id",
    "name": None,
    "place": "source_place_id",
    "text": "text",
    "date": "published_at",
    "stars": "rating",
}
PASSPORT = {
    "source": "synthetic",
    "reviewer_id_allowed": True,
    "rating_scale": [1, 5],
    "event_basis": "submitted",
}


def reviewer_fixture(db, directory, base, w, s, *, corrected=False, granted=True):
    directory.mkdir()
    common = {
        "id": "ctx-a",
        "author": AUTHOR,
        "name": "Пример",
        "place": "p1",
        "text": BODY,
        "date": "2026-03-01",
        "stars": 5 if corrected else 2,
    }
    rows = [
        common,
        common | {"id": "ctx-b", "place": "p2", "date": None, "stars": 4},
        common
        | {"id": "ctx-c", "author": AUTHOR + " ", "date": "bad", "stars": None, "text": None},
        common | {"id": "ctx-d", "author": None, "stars": 2},
        common | {"id": "ctx-e", "author": None, "stars": 2},
        common,
        common | {"id": "ctx-conflict"},
        common | {"id": "ctx-conflict", "stars": 3},
    ]
    inputs = json_input(directory, rows, FIELDS, PASSPORT)
    admitted = preview(
        (directory / "data").read_bytes(),
        "json",
        (directory / "mapping").read_bytes(),
        (directory / "passport").read_bytes(),
    )
    assert not admitted.allowed_in_browser("synthetic", {"p1", "p2"})
    result = call(base + inputs + ["--apply"])
    assert (
        result["input"],
        result["accepted"],
        result["quarantined"],
        result["confirmed_duplicates"],
    ) == (8, 5, 2, 1)
    digest = hashlib.sha256((directory / "data").read_bytes()).hexdigest()
    db.execute(
        """INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,author_context_allowed)
           VALUES (%s,%s,%s,true,%s)""",
        (w, s, digest, granted),
    )
    versions = [
        str(row[0])
        for row in db.execute(
            """SELECT r.version_id FROM snapshot s JOIN import_row r
           ON (r.workspace_id,r.source_id,r.batch_id)=(s.workspace_id,s.source_id,s.batch_id)
           WHERE s.id=%s AND r.status='accepted' ORDER BY r.position""",
            (result["snapshot_id"],),
        )
    ]
    return result["snapshot_id"], versions


def context_url(w, snap, version):
    return f"{ROOT}/workspaces/{w}/snapshots/{snap}/reviews/{version}/reviewer-context"


def test_reviewer_context_scope_permissions_and_versions(db, tmp_path, key, caplog):  # noqa: F811
    base, _, w, s = command(db, tmp_path)
    snap, versions = reviewer_fixture(db, tmp_path / "first", base, w, s, granted=False)
    newer, corrections = reviewer_fixture(db, tmp_path / "correction", base, w, s, corrected=True)
    assert corrections[0] != versions[0] and corrections[1:] == versions[1:]
    secret = secrets.token_urlsafe(24)
    owner = provision(db, "context-owner", secret, key, workspace=w)
    for same_workspace in (True, False):
        w2, project2, s2 = scope(
            db, workspace=w if same_workspace else None, source_key="synthetic"
        )
        for place in ("p1", "p2"):
            bind_place(db, w2, project2, s2, place)
        args = base.copy()
        args[1], args[3] = str(w2), str(s2)
        if not same_workspace:
            db.execute("UPDATE workspace SET delivery_object_root=%s WHERE id=%s", (args[5], w2))
            db.execute(
                "INSERT INTO workspace_membership SELECT %s,id,'analyst','active' FROM principal WHERE login='operator'",
                (w2,),
            )
        db.execute(
            "INSERT INTO delivery_authorization SELECT %s,%s,current_user,id FROM principal WHERE login='operator'",
            (w2, s2),
        )
        foreign_snap, foreign = reviewer_fixture(db, tmp_path / str(s2), args, w2, s2)
        if same_workspace:
            sibling_source, sibling_snap, sibling = s2, foreign_snap, foreign
    path = context_url(w, snap, versions[0])
    params = {"source_id": str(s)}
    with TestClient(create_app(), base_url=ORIGIN) as web:
        assert web.get(path, params=params).status_code == 401
        assert login(web, "context-owner", secret).status_code == 204
        caplog.set_level(logging.INFO, logger="reviewscope.access.requests")
        closed = capture_access(db)
        pending = web.get(path, params=params).json()
        assert pending["status"] == "AUTHOR_CONTEXT_NOT_ADMITTED" and pending["total"] is None
        assert pending["rows"] == [] and pending["metrics"] is None
        assert all(
            field
            == {
                "status": "not_available",
                "eligible": None,
                "total": None,
                "reason": pending["status"],
            }
            for field in pending["availability"].values()
        )
        db.execute(
            "UPDATE browser_admission SET author_context_allowed=true WHERE workspace_id=%s AND source_id=%s",
            (w, s),
        )
        assert capture_access(db) != closed
        response = web.get(path, params=params)
        assert response.status_code == 200, response.status_code
        full = response.json()
        assert full["method"] == "snapshot-reviewer-context-v1" and full["status"] == "computed"
        assert full["workspace_id"] == str(w) and full["source_id"] == str(s)
        assert full["snapshot_id"] == snap and full["anchor_version_id"] == versions[0]
        assert (
            full["manifest_sha256"]
            == db.execute("SELECT manifest_sha256 FROM snapshot WHERE id=%s", (snap,)).fetchone()[0]
        )
        assert full["availability"]["geography"] == {
            "reason": "GEO_METADATA_UNAVAILABLE",
            "status": "not_available",
            "eligible": 0,
            "total": 2,
        }
        assert full["availability"]["observed_dates"]["eligible"] == 1
        assert full["total"] == 2 and full["metrics"] == dict(
            places=2,
            rated=2,
            rating_mean=3,
            rating_counts={"2": 1, "4": 1},
            dated=1,
            first_day="2026-03-01",
            last_day="2026-03-01",
            with_text=2,
        )
        assert [r["version_id"] for r in full["rows"]] == versions[:2]
        assert [r["day"] for r in full["rows"]] == ["2026-03-01", None]
        assert all(r["specificity"] == specificity_score(BODY).value for r in full["rows"])
        assert (
            web.get(path, params=params | {"limit": 1, "offset": 1}).json()["rows"]
            == full["rows"][1:]
        )
        assert web.get(path, params=params | {"offset": 2}).json()["total"] == 2
        solo = web.get(context_url(w, snap, versions[2]), params=params).json()
        assert (
            solo["total"] == 1
            and solo["metrics"]["dated"]
            == solo["metrics"]["rated"]
            == solo["metrics"]["with_text"]
            == 0
        )
        assert (
            solo["metrics"]["rating_mean"]
            is solo["metrics"]["first_day"]
            is solo["rows"][0]["specificity"]
            is None
        )
        assert solo["availability"]["ratings"] == {
            "status": "insufficient_data",
            "eligible": 0,
            "total": 1,
            "reason": "NO_ELIGIBLE_RATINGS",
        }
        for missing in versions[3:]:
            part = web.get(context_url(w, snap, missing), params=params).json()
            assert part["status"] == "NO_REVIEWER_ID" and part["total"] is None and not part["rows"]
            assert part["availability"]["places"] == {
                "status": "not_available",
                "eligible": None,
                "total": None,
                "reason": "NO_REVIEWER_ID",
            }
        updated = web.get(context_url(w, newer, corrections[1]), params=params).json()
        assert [r["version_id"] for r in updated["rows"]] == corrections[:2]
        assert (
            updated["metrics"]["rating_mean"] == 4.5
            and updated["manifest_sha256"] != full["manifest_sha256"]
        )
        for route, query in (
            (context_url(w, newer, versions[0]), params),
            (context_url(w, snap, uuid4()), params),
            (context_url(w, sibling_snap, sibling[0]), params),
            (context_url(w, snap, versions[0]), {"source_id": str(sibling_source)}),
            (context_url(w2, foreign_snap, foreign[0]), {"source_id": str(s2)}),
        ):
            assert web.get(route, params=query).status_code == 404
        assert (
            web.get(
                context_url(w, sibling_snap, sibling[0]), params={"source_id": str(sibling_source)}
            ).json()["total"]
            == 2
        )
        assert web.get(path, params=params | {"run_id": str(uuid4())}).status_code == 422
        assert web.get(path, params=params | {"limit": 51}).status_code == 422
        witness = ledger_path()
        parked = witness.with_suffix(".parked")
        witness.rename(parked)
        try:
            denied = web.get(path, params=params)
            assert denied.status_code == 503 and "metrics" not in denied.json()
        finally:
            parked.rename(witness)
        assert AUTHOR not in response.text and BODY not in response.text
        assert AUTHOR not in caplog.text and BODY not in caplog.text
        # A completed SQL read holds the exact grant until transaction end. A writer must wait.
        with psycopg.connect(user="rs_runtime") as reader, psycopg.connect() as writer:
            reader.execute(
                "SELECT set_config('rs.session_proof',%s,true),set_config('rs.workspace',%s,true)",
                (session_proof(web.cookies[COOKIE], key), str(w)),
            )
            sql = "SELECT reviewer_context_page(%s,%s,%s,%s,0,1)"
            assert reader.execute(sql, (w, s, snap, versions[0])).fetchone()[0]["total"] == 2
            writer.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(psycopg.errors.LockNotAvailable):
                writer.execute(
                    "UPDATE browser_admission SET author_context_allowed=false WHERE workspace_id=%s AND source_id=%s",
                    (w, s),
                )
            writer.rollback()
            reader.commit()
            assert reader.execute(sql, (w, s, snap, versions[0])).fetchone()[0] is None
        db.execute(
            "UPDATE browser_admission SET author_context_allowed=false WHERE workspace_id=%s AND source_id=%s",
            (w, s),
        )
        assert web.get(path, params=params).json()["status"] == "AUTHOR_CONTEXT_NOT_ADMITTED"
        db.execute(
            "UPDATE browser_admission SET evidence_allowed=false WHERE workspace_id=%s AND source_id=%s",
            (w, s),
        )
        assert web.get(path, params=params).status_code == 404
        db.execute(
            "UPDATE workspace_membership SET role='viewer' WHERE workspace_id=%s AND principal_id=%s",
            (w, owner),
        )
        assert web.get(path, params=params).status_code == 403
        db.execute(
            "UPDATE workspace_membership SET state='inactive' WHERE workspace_id=%s AND principal_id=%s",
            (w, owner),
        )
        assert web.get(path, params=params).status_code == 404
        assert web.post(ROOT + "/session/logout", headers=CSRF).status_code == 204
        assert web.get(path, params=params).status_code == 401
    for name, allowed in (("rs_runtime", True), ("rs_worker", False), ("rs_upload", False)):
        with psycopg.connect(user=name) as role:
            assert role.execute(
                """SELECT has_function_privilege(current_user,'reviewer_context_page(uuid,uuid,uuid,uuid,integer,integer)','EXECUTE'),
                   has_column_privilege(current_user,'browser_admission','author_context_allowed','UPDATE')"""
            ).fetchone() == (allowed, False)


def test_incompatible_non_anchor_member_closes_whole_snapshot(db, tmp_path, key):  # noqa: F811
    base, _, w, s = command(db, tmp_path)
    # Model another writer's schema at insert time, before the ordinary hash and seal.
    db.execute("""
        CREATE FUNCTION fixture_normalization() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.normalization_version:='other-normalizer'; RETURN NEW; END $$;
        CREATE TRIGGER fixture_schema BEFORE INSERT ON review_version
        FOR EACH ROW WHEN (NEW.external_place_id='p2') EXECUTE FUNCTION fixture_normalization();
    """)
    snap, versions = reviewer_fixture(db, tmp_path / "mixed", base, w, s)
    labels = dict(
        db.execute("SELECT id::text,normalization_version FROM review_version").fetchall()
    )
    assert labels[versions[0]] == labels[versions[2]] == "canonical-preview-1"
    assert labels[versions[1]] == "other-normalizer"
    password = secrets.token_urlsafe(24)
    provision(db, "mixed-reader", password, key, workspace=w)
    with TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "mixed-reader", password).status_code == 204
        for index in (0, 2):
            denied = web.get(
                context_url(w, snap, versions[index]), params={"source_id": s, "limit": 1}
            )
            assert denied.status_code == 404 and "metrics" not in denied.json()


def test_unknown_reviewers_keep_exact_evidence_and_signals(db, tmp_path, key, monkeypatch):  # noqa: F811
    from operational.tests.test_browser_delivery import importer
    from operational.tests.test_deliveries import conflicting_json
    from operational.tests.test_signals import signal_job
    from operational.tests.test_worker import cli, enqueue

    base, _, w, s = command(db, tmp_path)
    secret = secrets.token_urlsafe(24)
    provision(db, "no-authors", secret, key, workspace=w, role="analyst")
    prefix = f"{ROOT}/workspaces/{w}"
    with importer(tmp_path, monkeypatch), TestClient(create_app(), base_url=ORIGIN) as web:
        assert login(web, "no-authors", secret).status_code == 204
        for args, file, accounting in (
            (csv("a"), FIX / "a.csv", (3, 3, 0, 0)),
            (csv("b"), FIX / "b.csv", (6, 6, 0, 0)),
            (conflicting_json(tmp_path), tmp_path / "data", (12, 6, 5, 1)),
        ):
            result = call(base + args + ["--apply"])
            assert (
                tuple(
                    result[k] for k in ("input", "accepted", "quarantined", "confirmed_duplicates")
                )
                == accounting
            )
            db.execute(
                "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed) VALUES(%s,%s,%s,true)",
                (w, s, hashlib.sha256(file.read_bytes()).hexdigest()),
            )
            snap = result["snapshot_id"]
            path = f"{prefix}/snapshots/{snap}"
            enqueue(web, prefix + "/runs", snap)
            assert cli("authorless-summary") == "succeeded"
            counts = web.get(path + "/summary-metrics").json()["metrics"]["eligibility"][
                "unknown_author"
            ]
            assert counts == {"numerator": accounting[1], "denominator": accounting[1]}
            run = signal_job(web, prefix, snap)
            assert cli("authorless-signals") == "succeeded"
            signals = web.get(path + "/signals", params={"source_id": s, "run_id": run}).json()[
                "result"
            ]
            rows = web.get(path + "/reviews").json()["rows"]
            by_version = {r["version_id"]: r for r in signals["reviews"]}
            assert len(rows) == len(by_version) == accounting[1]
            for row in rows:
                version = row["version_id"]
                part = web.get(context_url(w, snap, version), params={"source_id": s}).json()
                assert (
                    part["status"] == "NO_REVIEWER_ID"
                    and part["rows"] == []
                    and part["metrics"] is part["total"] is None
                )
                detail = web.get(path + "/reviews/" + version).json()["rows"][0]
                assert (
                    detail["identity_id"] == row["identity_id"]
                    and detail["version_no"] == row["version_no"]
                )
                assert by_version[version]["rating"] == detail["rating"]
                text = detail["full_text"]
                spec = by_version[version]["specificity"]
                if text and text.strip():
                    assert spec["heuristic_score"] == specificity_score(text).value
                else:
                    assert spec is None


def measure_page():
    """Called in a fresh process so the reported API high-water mark excludes test collection."""
    import sys
    from time import perf_counter

    from operational.worker import linux_peak_kib

    route, source_id, password = json.load(sys.stdin)
    with TestClient(create_app(), base_url=ORIGIN) as web, psycopg.connect() as inspect:
        assert login(web, "context-boundary", password).status_code == 204
        for offset in (0, 9950):
            start = perf_counter()
            response = web.get(
                route, params={"source_id": source_id, "offset": offset, "limit": 50}
            )
            elapsed = perf_counter() - start
            assert response.status_code == 200, response.status_code
            doc = response.json()
            assert doc["total"] == doc["metrics"]["with_text"] == 10000 and len(doc["rows"]) == 50
            assert [row["position"] for row in doc["rows"]] == list(range(offset + 1, offset + 51))
            assert all(row["specificity"] is not None for row in doc["rows"])
            (pid,) = inspect.execute(
                "SELECT pid FROM pg_stat_activity WHERE datname=current_database() AND usename='rs_runtime'"
            ).fetchone()
            print(
                json.dumps(
                    dict(
                        probe="reviewer-context-10k",
                        offset=offset,
                        processed=doc["total"],
                        excluded=0,
                        seconds=round(elapsed, 4),
                        page_bytes=len(response.content),
                        api_peak_kib=linux_peak_kib("self"),
                        pg_peak_kib=linux_peak_kib(pid),
                    )
                )
            )


def test_reviewer_context_resource_boundary(db, tmp_path, key):  # noqa: F811
    import subprocess
    import sys

    from operational.tests.test_deliveries import inputs
    from reviewscope.import_preview.contract import MAX_BYTES, MAX_ROWS

    base, _, w, s = command(db, tmp_path)
    header = b"id,author,body\n"
    width, extra = divmod(MAX_BYTES - len(header) - MAX_ROWS * len(b"00000,same,\n"), MAX_ROWS)
    data = tmp_path / "input.csv"
    with data.open("wb") as out:
        out.write(header)
        for i in range(MAX_ROWS):
            out.write(b"%05d,same,%s\n" % (i, b"x" * (width + (i < extra))))
    assert data.stat().st_size == MAX_BYTES
    mapping = tmp_path / "mapping"
    mapping.write_text(
        json.dumps(
            {
                "version": "context-1",
                "fields": {"id": "source_review_id", "author": "reviewer_id", "body": "text"},
            }
        )
    )
    passport = tmp_path / "passport"
    passport.write_text(
        json.dumps(
            {
                "delimiter": ",",
                "source": "synthetic",
                "source_place_id": "p1",
                "reviewer_id_allowed": True,
            }
        )
    )
    report = call(base + inputs(data, mapping, passport, "csv") + ["--apply"])
    assert report["accepted"] == report["input"] == MAX_ROWS
    snap = report["snapshot_id"]
    digest, anchor = db.execute(
        """SELECT b.file_sha256,r.version_id FROM snapshot s JOIN import_batch b
            ON (b.workspace_id,b.source_id,b.id)=(s.workspace_id,s.source_id,s.batch_id)
            JOIN import_row r ON (r.workspace_id,r.source_id,r.batch_id)=(s.workspace_id,s.source_id,s.batch_id)
            WHERE s.id=%s AND r.position=1""",
        (snap,),
    ).fetchone()
    db.execute(
        "INSERT INTO browser_admission(workspace_id,source_id,checksum,evidence_allowed,author_context_allowed) VALUES (%s,%s,%s,true,true)",
        (w, s, digest),
    )
    password = secrets.token_urlsafe(24)
    provision(db, "context-boundary", password, key, workspace=w)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from operational.tests.test_reviewer_context import measure_page; measure_page()",
        ],
        input=json.dumps([context_url(w, snap, anchor), str(s), password]),
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    for line in result.stdout.splitlines():
        measured = json.loads(line)
        assert measured["page_bytes"] < 65536
        print(line)
    assert db.execute("SELECT count(*) FROM analysis_run").fetchone() == (0,)
