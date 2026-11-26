"""Acceptance fixture on admitted PR-10/22 data. The reuse experiment has no runtime entry point."""

import hashlib
import json
from collections import Counter
from pathlib import Path
from threading import Event
from uuid import uuid4

from operational import decision_worker, worker
from operational.delivery_objects import verify_file
from operational.tests.test_access import key  # noqa: F401
from operational.tests.test_decision_storage import FIELDS, cabinet, enqueue, page
from operational.tests.test_deliveries import COVER, call, csv, json_input
from reviewscope.aspect_decisions import definitions

FIXTURE = Path("tests/data/monitors/history.json")
DELTA = Path("operational/monitor_delta.sql").read_text()


def decision(conn, reader, scope):
    code, run = enqueue(reader, scope)
    assert code == 202
    claim = conn.execute("SELECT * FROM claim_job()").fetchone()
    assert claim[1] == run and worker.process(conn, claim, Event()) == "succeeded"
    result = page(reader, scope, run)
    assert result["compute_state"] == "COMPLETE"
    return run, result


def delta(db, scope, before, after):
    return db.execute(
        DELTA, dict(workspace=scope[0], source=scope[1], previous=before, target=after)
    ).fetchall()


def experiment(monkeypatch, expected_hits, parent, calls):
    # Change only the input selection for the isolated test. Keep the real scorer, stage and publisher.
    full = decision_worker.run_batches
    cache = {row["version_id"]: row for row in parent["rows"]}

    def selected(params, rows, pulse, consume):
        assert (
            params["version_id"],
            params["digest"],
            params["profile_sha256"],
            params["threshold_set"],
        ) == (
            parent["definition"]["version_id"],
            parent["definition"]["digest"],
            parent["profile_sha256"],
            parent["threshold_set"],
        )
        misses = []
        hits = []
        for version, content_sha, text in rows:
            if version in expected_hits:
                row = cache[version]
                assert row["content_sha256"] == content_sha
                assert row["text_sha256"] == hashlib.sha256(text.encode()).hexdigest()
                hits.append(row)
            else:
                misses.append((version, content_sha, text))
        assert {row["version_id"] for row in hits} == expected_hits
        if hits:
            consume(hits)
        stats = full(params, misses, pulse, consume)
        assert stats["runtime"] == parent["runtime"]
        calls.append(stats["unique_scored"])
        return stats

    monkeypatch.setattr(decision_worker, "run_batches", selected)


def test_monitor_history_differential(db, tmp_path, key, monkeypatch):  # noqa: F811
    fixture = json.loads(FIXTURE.read_bytes())
    supported = definitions()[fixture["definition"]]
    with (
        cabinet(db, tmp_path, key, inputs=fixture["A"], definition=supported) as (
            scope,
            reader,
            _,
            root,
            _,
            _,
        ),
        worker.connect() as conn,
    ):
        w, src, a, aspect, version = scope
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

        def register(rows, coverage):
            (tmp_path / "coverage").write_text(json.dumps(coverage))
            command = [*args, *json_input(tmp_path, rows, FIELDS), "--apply"]
            delivery = call(command)
            replay = call(command)
            assert replay == delivery | {"outcome": "already_applied"}
            db.execute(
                """INSERT INTO browser_admission(workspace_id,source_id,checksum,
                   evidence_allowed,candidate_allowed,decision_allowed) VALUES(%s,%s,%s,true,true,true)
                   ON CONFLICT DO NOTHING""",
                (w, src, hashlib.sha256((tmp_path / "data").read_bytes()).hexdigest()),
            )
            return delivery["snapshot_id"]

        def verified_parent(run, result):
            assert db.execute("SELECT decision_manifest_valid(%s,%s)", (w, run)).fetchone()[0]
            verify_file(root / str(w) / "sha256", result["artifact_sha256"])

        run_a, first = decision(conn, reader, scope)
        verified_parent(run_a, first)
        assert (first["summary"]["N_accepted"], first["summary"]["N_evaluated"]) == (5, 4)
        b = register(fixture["B"], COVER | {"coverage": "partial"})
        scope_b = (w, src, b, aspect, version)
        pairs = delta(db, scope, a, b)
        assert not delta(db, scope, uuid4(), b)
        assert not delta(db, scope, a, uuid4())
        assert not delta(db, scope, a, a)
        assert not delta(db, (w, uuid4()), a, b)
        external = dict(
            db.execute(
                "SELECT id,external_review_id FROM review_identity WHERE workspace_id=%s AND source_id=%s",
                (w, src),
            ).fetchall()
        )
        assert sorted((external[i] or "", label) for i, _, _, label in pairs) == sorted(
            [
                ("R1", "unchanged_version"),
                ("R2", "changed_version"),
                ("R3", "not_observed_in_target"),
                ("R4", "unchanged_version"),
                ("R5", "newly_observed"),
                ("", "unlinked_observation"),
                ("", "unlinked_predecessor"),
            ]
        )
        for _, old, new, label in pairs:
            if label == "unchanged_version":
                assert old == new
            elif label == "changed_version":
                assert old and new and old != new
            else:
                assert (old is None) != (new is None)
        run_b, control = decision(conn, reader, scope_b)
        stats = control["summary"]
        assert [
            stats[k]
            for k in (
                "N_input",
                "N_accepted",
                "N_quarantine",
                "N_confirmed_duplicate",
                "N_excluded",
                "N_eligible",
                "N_evaluated",
            )
        ] == [7, 5, 1, 1, 1, 4, 4]
        assert page(reader, scope_b, run_a) is None
        calls = []
        with monkeypatch.context() as probe:
            experiment(
                probe,
                {str(new) for _, _, new, k in pairs if k == "unchanged_version"},
                first,
                calls,
            )
            incremental_id, mixed = decision(conn, reader, scope_b)
        assert incremental_id != run_b and calls == [3]
        for field in ("rows", "summary", "rows_sha256", "profile_sha256", "runtime"):
            assert mixed[field] == control[field]
        assert page(reader, scope, run_a) == first

        stable = [row for row in fixture["B"][:5] if row.get("id") in fixture["stable_C"]]
        c = register(stable, COVER)
        d = register(
            stable,
            COVER
            | {
                "mode": "cumulative",
                "coverage": "full",
                "basis": "synthetic delivery",
                "filters": {},
            },
        )
        stable_pairs = delta(db, scope, c, d)
        assert Counter(p[3] for p in stable_pairs) == {"unchanged_version": 4}
        assert Counter(p[3] for p in delta(db, scope, a, d))["not_observed_in_target"] == 1
        scope_c = (w, src, c, aspect, version)
        run_c, third = decision(conn, reader, scope_c)
        verified_parent(run_c, third)
        with monkeypatch.context() as probe:
            experiment(probe, {str(p[2]) for p in stable_pairs}, third, calls)
            run_d, fourth = decision(conn, reader, (w, src, d, aspect, version))
        assert calls == [3, 0] and run_c != run_d and c != d
        assert fourth["rows"] == third["rows"] and fourth["rows_sha256"] == third["rows_sha256"]
        assert fourth["summary"]["N_evaluated"] == 3
        print(
            json.dumps(
                dict(
                    snapshots=[a, b, c, d],
                    decision_ids=list(map(str, [run_a, run_b, incremental_id, run_c, run_d])),
                    pairs=[
                        [str(i), str(old) if old else None, str(new) if new else None, label]
                        for i, old, new, label in pairs
                    ],
                    rows_sha=[v["rows_sha256"] for v in [first, control, mixed, third, fourth]],
                    unique_scored=calls,
                )
            )
        )


def test_monitor_delta_on_original_deliveries(db, tmp_path):
    from operational.tests.test_deliveries import command

    args, _, w, src = command(db, tmp_path)
    a, b = [call([*args, *csv(name), "--apply"])["snapshot_id"] for name in ("a", "b")]
    assert Counter(row[3] for row in delta(db, (w, src), a, b)) == {
        "unchanged_version": 2,
        "changed_version": 1,
        "newly_observed": 2,
        "unlinked_observation": 1,
    }
