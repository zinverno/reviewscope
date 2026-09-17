"""Unit tests for the ingestion layer (SPEC.md §6, §37)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from reviewscope.ingestion.csv_adapter import CSVAdapter
from reviewscope.ingestion.json_adapter import JSONAdapter
from reviewscope.ingestion.normalize import (
    normalize_review,
    parse_date,
    parse_float,
    parse_int,
)

# ---------------------------------------------------------------------------
# Tolerant parsing helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("12.5", 12.5), ("12,5", 12.5), (" 7 ", 7.0), ("-3.25", -3.25)])
def test_parse_float_case(raw: str, expected: float) -> None:
    assert parse_float(raw) == expected


def test_parse_float_rejects_garbage() -> None:
    assert parse_float("abc") is None
    assert parse_float(None) is None
    assert parse_float("") is None


@pytest.mark.parametrize(
    "raw,expected",
    [("5", 5), ("5.0", 5), ("4,5", 4), ("★★★★★", None), ("5 звёзд", 5), (7.2, 7), ("  ", None)],
)
def test_parse_int_case(raw: object, expected: int | None) -> None:
    assert parse_int(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-03", datetime(2026, 9, 3)),
        ("2026-09-03T12:34:56", datetime(2026, 9, 3, 12, 34, 56)),
        ("03.09.2026", datetime(2026, 9, 3)),
        ("03.09.2026 18:40", datetime(2026, 9, 3, 18, 40)),
        ("2026.09.03", datetime(2026, 9, 3)),
        ("03/09/2026", datetime(2026, 9, 3)),
        ("2026/09/03", datetime(2026, 9, 3)),
    ],
)
def test_parse_date_formats(raw: str, expected: datetime) -> None:
    assert parse_date(raw) == expected


def test_parse_date_iso_with_zone() -> None:
    parsed = parse_date("2026-09-03T21:00:00+03:00")
    assert parsed is not None
    assert parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed).total_seconds() == 0


def test_parse_date_rejects_garbage() -> None:
    assert parse_date("not a date") is None
    assert parse_date(None) is None
    assert parse_date("") is None


# ---------------------------------------------------------------------------
# normalize_review
# ---------------------------------------------------------------------------


def test_normalize_review_happy_path() -> None:
    review, warnings = normalize_review(
        {
            "review_id": "r1",
            "place_id": "p1",
            "place_name": "Кофейня",
            "place_category": "coffee",
            "reviewer_id": "u1",
            "reviewer_name": "Иванов",
            "rating": "5",
            "text": "Отлично!",
            "published_at": "03.09.2026",
            "city": "Москва",
            "region": "МО",
            "country": "RU",
            "latitude": "55.75",
            "longitude": "37.61",
        }
    )
    assert review is not None
    assert review.rating == 5
    assert review.published_at == "2026-09-03T00:00:00"
    assert review.latitude == 55.75
    assert warnings == []


def test_normalize_review_missing_optional_fields_is_warning_only() -> None:
    review, warnings = normalize_review(
        {"review_id": "r1", "place_id": "p1", "reviewer_id": "u1"}
    )
    assert review is not None
    assert review.text is None
    assert review.reviewer_name is None
    assert any("missing review text" in w for w in warnings)
    assert any("rating" in w for w in warnings)


def test_normalize_review_missing_required_key_is_skipped() -> None:
    review, warnings = normalize_review({"place_id": "p1", "reviewer_id": "u1"})
    assert review is None
    assert "review_id" in warnings[0]


def test_normalize_review_bad_rating_dropped() -> None:
    review, warnings = normalize_review(
        {"review_id": "r1", "place_id": "p1", "reviewer_id": "u1", "rating": "9"}
    )
    assert review is not None
    assert review.rating is None
    assert any("rating 9 out of 1..5" in w for w in warnings)


def test_normalize_review_alias_column_names() -> None:
    review, _ = normalize_review(
        {
            "id": "r1",
            "org_id": "p1",
            "user_id": "u1",
            "stars": "4",
            "comment": "Ok",
        }
    )
    assert review is not None
    assert review.review_id == "r1" and review.place_id == "p1" and review.rating == 4


def test_normalize_review_invalid_coordinate_dropped() -> None:
    review, warnings = normalize_review(
        {
            "review_id": "r1",
            "place_id": "p1",
            "reviewer_id": "u1",
            "latitude": "999",
            "longitude": "37.6",
        }
    )
    assert review is not None
    assert review.latitude is None
    assert review.longitude == 37.6
    assert any("latitude" in w for w in warnings)


def test_normalize_review_fingerprint_stable() -> None:
    a, _ = normalize_review({"review_id": "r1", "place_id": "p1", "reviewer_id": "u1", "text": "  Отлично! "})
    b, _ = normalize_review({"review_id": "r2", "place_id": "p2", "reviewer_id": "u2", "text": "отлично!"})
    assert a is not None and b is not None
    assert a.fingerprint() == b.fingerprint()


# ---------------------------------------------------------------------------
# CSV adapter
# ---------------------------------------------------------------------------


CSV_HEADER = ("review_id,place_id,reviewer_id,rating,text,published_at,city\n"
              "r1,p1,u1,5,Отлично!,2026-09-03,Москва\n"
              "r2,p1,u2,3,Нормально,01.09.2026,Казань\n")


def test_csv_adapter_basic(tmp_path: Path) -> None:
    path = tmp_path / "reviews.csv"
    path.write_text(CSV_HEADER, encoding="utf-8")
    result = CSVAdapter().load(path)
    assert result.report.valid == 2
    assert result.report.skipped == 0
    assert len(result.reviews) == 2
    assert result.reviews[1].rating == 3


def test_csv_adapter_bad_row_does_not_break_import(tmp_path: Path) -> None:
    # Row #4 has an empty review_id (the required key) -> skipped, not fatal.
    content = CSV_HEADER + ",p1,u1,5,текст без id\n"
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    result = CSVAdapter().load(path)
    assert len(result.reviews) == 2
    assert result.report.skipped == 1
    assert "row #4" in result.report.warnings[0]


def test_csv_adapter_utf8_bom_and_cp1251(tmp_path: Path) -> None:
    bom = tmp_path / "bom.csv"
    bom.write_text("\ufeff" + CSV_HEADER, encoding="utf-8")
    assert CSVAdapter().load(bom).report.valid == 2

    cp1251_path = tmp_path / "cp1251.csv"
    cp1251_path.write_bytes("review_id,place_id,reviewer_id,rating,text\nr1,p1,u1,5,Отлично\n".encode("cp1251"))
    assert CSVAdapter().load(cp1251_path).report.valid == 1


def test_csv_adapter_validation_report_display(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    path.write_text(CSV_HEADER, encoding="utf-8")
    report = CSVAdapter().load(path).report
    text = report.summary_display()
    assert "Imported: 2" in text and "Valid: 2" in text and "Skipped: 0" in text


# ---------------------------------------------------------------------------
# JSON adapter
# ---------------------------------------------------------------------------


def _records() -> list[dict]:
    return [
        {"review_id": "r1", "place_id": "p1", "reviewer_id": "u1", "rating": 5, "text": "Great", "published_at": "2026-09-03"},
        {"review_id": "r2", "place_id": "p1", "reviewer_id": "u2", "rating": 2, "text": "Bad", "published_at": "03.09.2026"},
    ]


def test_json_adapter_list(tmp_path: Path) -> None:
    path = tmp_path / "reviews.json"
    path.write_text(json.dumps(_records()), encoding="utf-8")
    result = JSONAdapter().load(path)
    assert result.report.valid == 2
    assert len(result.reviews) == 2


def test_json_adapter_wrapped(tmp_path: Path) -> None:
    path = tmp_path / "wrapped.json"
    path.write_text(json.dumps({"reviews": _records()}), encoding="utf-8")
    assert JSONAdapter().load(path).report.valid == 2


def test_json_adapter_non_object_records_skipped(tmp_path: Path) -> None:
    path = tmp_path / "mixed.json"
    # First record lacks required keys -> skipped. Integer is non-object -> dropped silently.
    path.write_text(json.dumps([{"foo": "bar"}] + _records() + [42]), encoding="utf-8")
    result = JSONAdapter().load(path)
    assert result.report.valid == 2
    assert result.report.skipped == 1
    assert result.report.total_rows == 3


def test_json_adapter_duplicate_ids_deduped(tmp_path: Path) -> None:
    path = tmp_path / "dup.json"
    path.write_text(json.dumps(_records() + [_records()[0]]), encoding="utf-8")
    result = JSONAdapter().load(path)
    assert result.report.valid == 2
