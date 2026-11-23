"""Synthetic import contract. Run independently of either database and the model backend."""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from reviewscope.import_preview import preview
from reviewscope.import_preview.contract import (
    MAX_BYTES,
    MAX_COLUMNS,
    MAX_DEPTH,
    MAX_FIELD,
    MAX_ROWS,
    PreviewError,
)

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/data/fixtures/import_preview"
PASSPORT = {
    "source": "synthetic",
    "source_place_id": "p1",
    "rating_scale": [1, 5],
    "event_basis": "published",
}


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def mapping(fields):
    return encode({"version": "test", "fields": fields})


def table(data, fields=None, fmt="json", **options):
    if fields is None:
        fields = {"body": "text"}
    passport = PASSPORT | options
    if fmt == "csv":
        passport.setdefault("delimiter", ",")
    return preview(
        data if isinstance(data, bytes) else encode(data), fmt, mapping(fields), encode(passport)
    )


def test_pilot_contract():
    markdown = (ROOT / "docs/development/pilot/data-contract.md").read_text()
    results = []
    for name, size in (("a", 3), ("b", 6)):
        data = (FIX / f"{name}.csv").read_bytes()
        assert data.decode() in markdown
        args = (
            data,
            "csv",
            (FIX / "mapping.json").read_bytes(),
            (FIX / f"{name}.passport.json").read_bytes(),
        )
        result = preview(*args)
        assert result == preview(*args)
        assert result.summary.total_input_rows == result.summary.accepted == size
        assert result.summary.quarantined == result.summary.confirmed_duplicates == 0
        assert result.summary.missing_author_count == size
        assert (
            result.summary.missing_date_count
            == result.summary.missing_text_count
            == result.summary.rating_excluded_count
            == 0
        )
        assert all(
            row.reviewer_id is None and not row.eligible_for["reviewer_features"]
            for row in result.rows
        )
        assert result.rows[0].event.value == "2026-09-08T14:10:00+00:00"
        assert result.passport.observed_at.offset == "+03:00"
        assert result.summary.reason_counts == (
            {"missing_reviewer": 3}
            if name == "a"
            else {"missing_reviewer": 6, "missing_review_id": 1}
        )
        results.append(result)
    a, b = results
    assert a.rows[0].source_review_id == b.rows[0].source_review_id == "r1"
    assert a.rows[0].text != b.rows[0].text and a.fingerprint != b.fingerprint
    assert b.rows[4].source_position == 5 and b.rows[4].source_review_id is None


def test_csv_encoding_quoting_and_width():
    text = 'body,id\r\n"Раз, ""два""\r\nтри",0009\r\nдва,id2,ещё\r\n\r\n'
    fields = {"body": "text", "id": "source_review_id"}
    for codec in ("utf-8-sig", "cp1251"):
        kwargs = {} if codec == "utf-8-sig" else {"encoding": codec}
        result = table(text.encode(codec), fields, "csv", **kwargs)
        first, extra, blank = result.rows
        assert first.text == 'Раз, "два"\r\nтри' and first.source_review_id == "0009"
        assert first.field_provenance["source"] == "passport:source"
        assert first.field_provenance["text"] == "field:body"
        assert (extra.source_position, blank.source_position) == (2, 3)
        assert extra.reasons == blank.reasons == ["row_width"]
        assert result.summary.accepted == 1 and result.summary.quarantined == 2
    with pytest.raises(PreviewError) as error:
        table(text.encode("cp1251"), fields, "csv")
    assert error.value.code == "invalid_encoding"


@pytest.mark.parametrize("wrapped", [True, False])
def test_scoped_identity_and_conflicts(wrapped):
    def item(source="s", place="p", rid="r1", text="одинаково"):
        return {"src": source, "loc": place, "id": rid, "body": text}

    original = item()
    data = [
        original,
        original,
        item(text="изменено"),
        item(rid="r2"),
        item(rid="r2"),
        item(source="s2"),
        item(place="p2"),
        item(rid=None),
        item(rid=None),
        False,
        item(rid="90071992547409931234"),
        item(rid=90071992547409931234),
    ]
    fields = {"src": "source", "loc": "source_place_id", "id": "source_review_id", "body": "text"}
    result = preview(encode({"reviews": data} if wrapped else data), "json", mapping(fields), b"{}")
    rows = result.rows
    assert [r.related_position for r in rows[:3]] == [3, 3, 1]
    assert all(r.reasons == ["conflicting_id"] for r in rows[:3])
    assert rows[4].related_position == 4 and rows[4].reasons == ["confirmed_duplicate"]
    assert [r.status for r in rows[5:9]] == ["accepted"] * 4
    assert rows[9].reasons == ["invalid_json_item"]
    assert rows[10].source_review_id == "90071992547409931234"
    assert rows[11].reasons == ["invalid_field_type"]
    assert (
        result.summary.total_input_rows,
        result.summary.accepted,
        result.summary.quarantined,
        result.summary.confirmed_duplicates,
    ) == (12, 6, 5, 1)
    assert all(not any(r.eligible_for.values()) for r in rows if r.status != "accepted")


def test_invalid_elements_and_sparse_objects():
    assert table([None, 17, []]).summary.reason_counts == {"invalid_json_item": 3}
    result = table(
        [{"body": None}, {"id": "two"}, {"body": "", "extra": "keep private"}],
        {"body": "text", "id": "source_review_id"},
    )
    assert [r.text for r in result.rows] == [None, None, ""]
    assert result.rows[2].reasons == ["unexpected_column"]
    assert (
        "text" in result.rows[0].field_provenance and "text" not in result.rows[1].field_provenance
    )
    assert table([{"body": "literal =SUM(2,3)"}]).rows[0].text == "literal =SUM(2,3)"


@pytest.mark.parametrize(
    ("raw", "options", "kind", "value", "precision", "warning"),
    [
        (
            "2026-01-02T00:30:00.120+03:00",
            {},
            "instant",
            "2026-01-01T21:30:00.120000+00:00",
            "fraction-3",
            None,
        ),
        ("2026-01-02T00:30Z", {}, "instant", "2026-01-02T00:30:00+00:00", "minute", None),
        ("2026-01-02T00:30-05:00", {}, "instant", "2026-01-02T05:30:00+00:00", "minute", None),
        ("2026-01-02", {}, "date", "2026-01-02", "day", None),
        ("02.01.2026", {"local_date": "DD.MM.YYYY"}, "date", "2026-01-02", "day", None),
        ("02.01.2026", {}, "invalid", None, None, "invalid_date"),
        ("2026-01-02T01:00", {}, "local", "2026-01-02T01:00:00", "minute", "unknown_timezone"),
        (
            "2026-01-02T01:00",
            {"timezone": "Europe/Berlin"},
            "instant",
            "2026-01-02T00:00:00+00:00",
            "minute",
            None,
        ),
        (
            "2026-10-25T02:30",
            {"timezone": "Europe/Berlin"},
            "local",
            "2026-10-25T02:30:00",
            "minute",
            "ambiguous_time",
        ),
        (
            "2026-03-29T02:30",
            {"timezone": "Europe/Berlin"},
            "local",
            "2026-03-29T02:30:00",
            "minute",
            "nonexistent_time",
        ),
        ("2026-02-30", {}, "invalid", None, None, "invalid_date"),
        ("2026-01-01T01:01+00:99", {}, "invalid", None, None, "invalid_date"),
        (None, {}, "missing", None, None, "missing_date"),
    ],
)
def test_events(raw, options, kind, value, precision, warning):
    r = table([{"dt": raw}], {"dt": "published_at"}, **options).rows[0]
    assert r.status == "accepted" and r.event.raw == raw
    assert (r.event.kind, r.event.value, r.event.precision) == (kind, value, precision)
    assert r.eligible_for["dated_comparison"] == (kind in {"instant", "date"})
    if warning:
        assert warning in r.warnings


@pytest.mark.parametrize("basis", ["edited", "unknown", "submitted"])
def test_event_basis(basis):
    result = table(
        [{"dt": "2026-01-01"}],
        {"dt": "published_at"},
        event_basis=basis,
        observed_at="2026-02-16T10:00:00Z",
    )
    row = result.rows[0]
    assert row.event.value == "2026-01-01" and result.passport.event_basis == basis
    assert row.eligible_for["dated_comparison"] == (basis == "submitted")
    assert ("ineligible_event_basis" in row.warnings) == (basis != "submitted")


@pytest.mark.parametrize(
    ("raw", "scale", "stars", "warning"),
    [
        (5, [1, 5], 5, None),
        ("3.00", [1, 5], 3, None),
        (None, [1, 5], None, "missing_rating"),
        ("", [1, 5], None, "missing_rating"),
        (0, [1, 5], None, "invalid_rating"),
        (0, [0, 10], None, "unmapped_scale"),
        (8, [1, 10], None, "unmapped_scale"),
        (2, None, None, "unmapped_scale"),
        ("4.5", [1, 5], None, "invalid_rating"),
        (False, [1, 5], None, "invalid_rating"),
        ("5 звёзд", [1, 5], None, "invalid_rating"),
    ],
)
def test_field_eligibility(raw, scale, stars, warning):
    result = table(
        [{"body": "  жалоба  ", "v": raw}, {"body": " \n", "v": 4}],
        {"body": "text", "v": "rating"},
        rating_scale=scale,
    )
    row, blank = result.rows
    assert row.status == blank.status == "accepted"
    assert row.eligible_for["text_analysis"] and not blank.eligible_for["text_analysis"]
    assert row.text == "  жалоба  " and blank.text == " \n"
    assert row.rating_1_to_5 == stars and row.eligible_for["rating"] == (stars is not None)
    if warning:
        assert warning in row.warnings
    assert result.summary.missing_text_count == 1


def test_binding_permission_and_url():
    fields = {
        "source": "source",
        "observed": "observed_at",
        "author": "reviewer_id",
        "url": "source_url",
    }
    obj = {
        "source": "wrong",
        "observed": "2026-02-14T00:00:00Z",
        "author": "87",
        "url": "https://example.org/r/91",
    }
    options = {"observed_at": "2026-02-15T00:00:00Z", "reviewer_id_allowed": True}
    r = table([obj], fields, **options).rows[0]
    assert r.reasons == ["binding_mismatch", "observation_mismatch"]
    assert r.source_url == obj["url"] and r.reviewer_id == "87"
    for url in (
        "https://u:p@example.org/r",
        "https://example.org/r?x=1",
        "https://example.org:99999/r",
        "https://example.org/r#info",
        "file:///p",
        "https://example.org/r;signature=private",
    ):
        r = table([{"url": url}], {"url": "source_url"}).rows[0]
        assert r.source_url is None and "invalid_url" in r.warnings
    with pytest.raises(PreviewError) as error:
        table([obj], fields, **(options | {"reviewer_id_allowed": False}))
    assert error.value.code == "invalid_passport"
    r = preview(b'[{"body":"a"}]', "json", mapping({"body": "text"}), b"{}").rows[0]
    assert r.reasons == ["missing_source", "missing_place"]
    r = table([{"author": "u1"}], {"author": "reviewer_id"}, reviewer_id_allowed=True).rows[0]
    assert r.eligible_for["reviewer_features"]


@pytest.mark.parametrize(
    ("data", "fmt", "fields", "code"),
    [
        (b"", "csv", {"t": "text"}, "empty_input"),
        (b"\xff", "json", {"t": "text"}, "invalid_encoding"),
        (b'[{"t":"\\ud800"}]', "json", {"t": "text"}, "invalid_json"),
        (b"a,a\n1,2", "csv", {"a": "text"}, "duplicate_header"),
        (b"a,\n1,2", "csv", {"a": "text"}, "invalid_header"),
        (b'a\n"unclosed', "csv", {"a": "text"}, "invalid_csv"),
        (b"a\n1", "csv", {"b": "text"}, "unknown_column"),
        (b"a,b\n1,2", "csv", {"a": "text"}, "unexpected_column"),
        (b"a,b\n1,2", "csv", {"a": "text", "b": "text"}, "conflicting_mapping"),
        (b"a,b\n1,2", "csv", {"a": "expression(1)"}, "invalid_mapping"),
        (b'{"r1":{"t":1}}', "json", {"t": "text"}, "invalid_shape"),
        (b'{"reviews":[],"extra":1}', "json", {"t": "text"}, "invalid_shape"),
        (b'[{"t":"a","t":"b"}]', "json", {"t": "text"}, "duplicate_key"),
        (b'[{"t":NaN}]', "json", {"t": "text"}, "invalid_json"),
        (b'[{"t":"a"}]', "json", {"x": "text"}, "unknown_column"),
        (b"[" * (MAX_DEPTH + 1) + b"]" * (MAX_DEPTH + 1), "json", {"t": "text"}, "over_limit"),
        (b"[{}" + b",{}" * MAX_ROWS + b"]", "json", {"t": "text"}, "over_limit"),
        (b"a\n" + b"b\n" * (MAX_ROWS + 1), "csv", {"a": "text"}, "over_limit"),
        (b"a\n" + b"b" * (MAX_FIELD + 1), "csv", {"a": "text"}, "over_limit"),
        (b'[{"t":"' + b"b" * (MAX_FIELD + 1) + b'"}]', "json", {"t": "text"}, "over_limit"),
        (b"a\n" + b"," * MAX_COLUMNS, "csv", {"a": "text"}, "over_limit"),
        (b"x" * (MAX_BYTES + 1), "json", {"t": "text"}, "over_limit"),
    ],
    ids=lambda value: f"{len(value)}-bytes" if isinstance(value, bytes) else None,
)
def test_fatal_outcomes(data, fmt, fields, code):
    with pytest.raises(PreviewError) as error:
        table(data, fields, fmt)
    assert error.value.code == code


def test_explicit_zero_offset_keeps_its_spelling():
    result = table(
        [{"dt": "2026-05-15T10:30-00:00"}], {"dt": "published_at"}, timezone="Europe/Moscow"
    )
    event = result.rows[0].event
    assert event.value == "2026-05-15T10:30:00+00:00" and event.offset == "-00:00"


def test_profile_is_literal_and_hashed():
    with pytest.raises(PreviewError) as error:
        preview(
            b"a\nb",
            "csv",
            b'{"version":"v","fields":{"a":"text","a":"source"}}',
            b'{"delimiter":","}',
        )
    assert error.value.code == "duplicate_key"
    a = table([{"body": "one"}])
    b = table([{"body": "one"}], source="other")
    c = preview(b'[{"body": "one"}]', "json", mapping({"body": "text"}) + b"\n", encode(PASSPORT))
    assert a.file_sha256 == b.file_sha256 == c.file_sha256
    assert len({a.fingerprint, b.fingerprint, c.fingerprint}) == 3
    assert a.mapping_sha256 != c.mapping_sha256
    assert asdict(a.rows[0]) == asdict(c.rows[0])


def test_cli_report_privacy_and_retry(tmp_path):
    # A disposable checkout layout gives the CLI a private output root even under python -IS.
    from shutil import copytree

    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/preview_import.py").write_bytes(
        (ROOT / "scripts/preview_import.py").read_bytes()
    )
    copytree(ROOT / "src/reviewscope/import_preview", tmp_path / "src/reviewscope/import_preview")
    (tmp_path / "src/reviewscope/__init__.py").write_text("")
    marker = "SYNTHETIC_PRIVATE_MARKER"
    paths = [tmp_path / (marker + key) for key in ("input", "mapping", "passport")]
    paths[0].write_bytes(encode([{"private": marker}]))
    paths[1].write_bytes(encode({"version": marker, "fields": {"private": "text"}}))
    paths[2].write_bytes(encode(PASSPORT))
    out = tmp_path / "validation_data/private/example"
    cmd = [
        sys.executable,
        "-IS",
        "scripts/preview_import.py",
        "--format",
        "json",
        "--input",
        paths[0],
        "--mapping",
        paths[1],
        "--passport",
        paths[2],
        "--out-dir",
        out,
    ]

    def run(command, expected_code):
        p = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=False)
        assert p.returncode == expected_code
        assert all(
            secret not in p.stdout + p.stderr for secret in (marker, str(tmp_path), "Traceback")
        )
        return json.loads(p.stdout if expected_code == 0 else p.stderr)

    public = run(cmd, 0)
    assert public["accepted"] == public["total_input_rows"] == 1
    assert public["warning_count"] == 4
    (target,) = out.iterdir()
    assert target.stat().st_mode & 0o777 == 0o600
    assert out.stat().st_mode & 0o777 == 0o700
    saved = json.loads(target.read_bytes())
    assert saved["rows"][0]["text"] == marker and saved["rows"][0]["eligible_for"]["text_analysis"]
    assert saved["delivery"]["messages"].keys() == public["reason_counts"].keys()
    original = target.stat()
    assert run(cmd, 0) == public and target.stat().st_ino == original.st_ino
    target.write_text("preserved")
    assert run(cmd, 2)["error"] == "private_output" and target.read_text() == "preserved"
    link = out.parent / "link"
    link.symlink_to(tmp_path, target_is_directory=True)
    assert run(cmd[:-1] + [link], 2)["error"] == "private_output"
    assert run(cmd[:-1] + [tmp_path], 2)["error"] == "private_output"
    assert run(cmd + [marker], 2)["error"] == "invalid_arguments"
    paths[0].write_bytes(b'[{"wrong":"private"}]')
    assert run(cmd, 2)["error"] == "unknown_column"
    paths[0].unlink()
    assert run(cmd, 2)["error"] == "io_error"
    if hasattr(os, "mkfifo"):
        os.mkfifo(paths[0])
        assert run(cmd, 2)["error"] == "io_error"


def test_conflict_in_ignored_or_extra_cells_includes_first_row():
    fields = {"id": "source_review_id", "unused": None}
    result = table(b"id,unused\nr,x\nr,y\ns,z,extra\ns,z,second\nq,w,1\nq,w,1\n", fields, "csv")
    assert [r.status for r in result.rows] == ["quarantined"] * 6
    assert [r.related_position for r in result.rows] == [2, 1, 4, 3, None, None]
    assert result.summary.reason_counts["conflicting_id"] == 4
    assert result.rows[-1].reasons == ["row_width"]
    r = table(b'[{"id":"r","unused":1},{"unused":"1","id":"r"}]', fields)
    assert r.summary.quarantined == 2 and r.summary.confirmed_duplicates == 0


@pytest.mark.parametrize(
    ("fmt", "opts", "code"),
    [
        ("xlsx", {}, "invalid_format"),
        ("json", {"encoding": "cp1251"}, "invalid_passport"),
        ("csv", {"encoding": "latin-1"}, "invalid_passport"),
        ("csv", {"delimiter": None}, "invalid_passport"),
        ("json", {"timezone": "No/Such_Zone"}, "invalid_passport"),
        ("json", {"rating_scale": [True, 5]}, "invalid_passport"),
        ("json", {"observed_at": "2026-01-01"}, "invalid_passport"),
    ],
)
def test_invalid_configuration(fmt, opts, code):
    with pytest.raises(PreviewError) as error:
        table(b"[]", fmt=fmt, **opts)
    assert error.value.code == code


def test_cli_has_no_network_store_or_backend_dependencies():
    script = """
import runpy, sys
def guard(event, args):
    if event.startswith(('socket.', 'sqlite3.')):
        raise AssertionError(event)
    if event == 'import' and args[0].split('.')[0] in {
        'torch', 'pandas', 'duckdb', 'psycopg', 'sqlalchemy', 'sentence_transformers','transformers'
    }:
        raise AssertionError('backend import')
sys.addaudithook(guard)
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
"""
    run = subprocess.run(
        [
            sys.executable,
            "-IS",
            "-c",
            script,
            "scripts/preview_import.py",
            "--input",
            FIX / "b.csv",
            "--format",
            "csv",
            "--mapping",
            FIX / "mapping.json",
            "--passport",
            FIX / "b.passport.json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    assert json.loads(run.stdout)["accepted"] == 6 and not run.stderr


def test_column_and_field_limits_are_inclusive():
    names = [f"c{i}" for i in range(MAX_COLUMNS)]
    fields = dict.fromkeys(names)
    fields["c0"] = "text"
    obj = dict.fromkeys(names, "")
    obj["c0"] = "я" * MAX_FIELD
    result = table([obj], fields)
    assert result.summary.accepted == 1 and result.rows[0].text == obj["c0"]
    with pytest.raises(PreviewError) as error:
        table([obj | {"extra": ""}], fields)
    assert error.value.code == "over_limit"


def test_total_json_structure_budget():
    # All per-row column/depth limits fit. The aggregate object allocation still has a cap.
    data = b"[" + b",".join([b'{"body":[' + b",".join([b"0"] * 64) + b"]}"] * 4000) + b"]"
    with pytest.raises(PreviewError) as error:
        table(data)
    assert error.value.code == "over_limit"
