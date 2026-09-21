"""Deterministic tests for the Yandex Geo Reviews 2023 validation-corpus converter.

The converter runs against the official TSKV release, so these tests feed it a
tiny synthetic TSKV dump (never the real corpus, which stays gitignored).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from reviewscope.ingestion.csv_adapter import CSVAdapter

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_MODULE_NAME = "prepare_yandex_geo_validation"


def _load_script():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPTS / f"{_MODULE_NAME}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


ymod = _load_script()


def _tskv_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def _tskv_row(**fields: str) -> str:
    parts = ["tskv"]
    for key in ("address", "name_ru", "rubrics", "rating", "text"):
        value = fields.get(key, "")
        parts.append(f"{key}={_tskv_escape(value)}")
    return "\t".join(parts)


def render_tskv() -> str:
    """Render a fixed synthetic TSKV dump shared by every test in this file."""
    rows: list[dict[str, str]] = [
        # --- restaurant/cafe category ---
        *(
            dict(
                address="Москва, ул. Тестовая, 1",
                name_ru="Ресторан Солнце",
                rubrics="Ресторан",
                rating=str((i % 5) + 1),
                text=f"Солнце отзыв {i}",
            )
            for i in range(10)
        ),
        *(
            dict(
                address="Москва, пр-т Мира, 2",
                name_ru="Кафе Утро",
                rubrics="Кафе;Кофейня",
                rating="5",
                text=f"Утро отзыв {i}",
            )
            for i in range(20)
        ),
        # drink venue: must NOT be a restaurant/cafe
        *(
            dict(
                address="Москва, ул. Ночная, 3",
                name_ru="Бар Ночь",
                rubrics="Бар, паб;Ночной клуб",
                rating="4",
                text=f"Бар отзыв {i}",
            )
            for i in range(3)
        ),
        # --- hotel category ---
        *(
            dict(
                address="Москва, Театральная, 3",
                name_ru="Гостиница Центр",
                rubrics="Гостиница",
                rating="4",
                text=f"Отель отзыв {i}",
            )
            for i in range(15)
        ),
        *(
            dict(
                address="Сочи, ул. Морская, 4",
                name_ru="Парк-Отель Юг",
                rubrics="Парк-отель",
                rating="5",
                text=f"Юг отзыв {i}",
            )
            for i in range(10)
        ),
        # hotel booking broker: must NOT be a hotel
        *(
            dict(
                address="Москва, ул. Служб, 5",
                name_ru="Бронирование гостиниц Сервис",
                rubrics="Бронирование гостиниц;Информационный интернет-сайт",
                rating="4",
                text=f"Сервис отзыв {i}",
            )
            for i in range(4)
        ),
        # multi-category org: must be excluded
        *(
            dict(
                address="Ялта, наб. Гостиничная, 7",
                name_ru="Отель и ресторан",
                rubrics="Гостиница;Ресторан",
                rating="3",
                text=f"Гибрид отзыв {i}",
            )
            for i in range(6)
        ),
        # --- pharmacy category ---
        *(
            dict(
                address="СПб, ул. Большая, 5",
                name_ru="Аптека 24",
                rubrics="Аптека",
                rating="5",
                text=f"Плюс отзыв {i}",
            )
            for i in range(8)
        ),
        *(
            dict(
                address="Казань, ул. Ленина, 6",
                name_ru="Зеленая аптека",
                rubrics="Аптека",
                rating="4",
                text=f"Зелень отзыв {i}",
            )
            for i in range(7)
        ),
        *(
            dict(
                address="Екатеринбург, пр-т Юности, 8",
                name_ru="Ютека Онлайн",
                rubrics="Аптека;Доставка",
                rating="3",
                text=f"Онлайн отзыв {i}",
            )
            for i in range(5)
        ),
        # veterinary pharmacy: must NOT be a human pharmacy
        *(
            dict(
                address="Москва, ул. Зоопарковая, 9",
                name_ru="Ветаптека Плюс",
                rubrics="Ветеринарная аптека",
                rating="5",
                text=f"Вет отзыв {i}",
            )
            for i in range(4)
        ),
        # --- shopping center category ---
        *(
            dict(
                address="Москва, ул. Галерейная, 1",
                name_ru="ТЦ Галерея",
                rubrics="Торговый центр",
                rating=str((i % 5) + 1),
                text=f"Галерея отзыв {i}",
            )
            for i in range(14)
        ),
        *(
            dict(
                address="Москва, ул. Сатурновая, 2",
                name_ru="ТЦ Сатурн",
                rubrics="Торговый центр",
                rating=str((i % 5) + 1),
                text=f"Сатурн отзыв {i}",
            )
            for i in range(10)
        ),
        # --- junk rows (skipped) ---
        dict(
            address="Москва, ул. Пустая, 10",
            name_ru="Скрытый",
            rubrics="Ресторан",
            rating="0",
            text="ноль",
        ),
        dict(
            address="Москва, ул. Пустая, 11",
            name_ru="Скрытый Два",
            rubrics="Кафе",
            rating="0",
            text="ноль два",
        ),
        dict(
            address="Москва, ул. Пустая, 12",
            name_ru="Без текста",
            rubrics="Ресторан",
            rating="5",
            text="",
        ),
    ]
    # one review carries a real newline, which the renderer escapes TSKV-style
    rows[0]["text"] = "Солнце отзыв 0\\nвторая строка"
    return "".join(_tskv_row(**row) + "\n" for row in rows)


@pytest.fixture(scope="module")
def organizations():
    text = render_tskv()
    records = ymod.load_tabular_text(text, "tskv")
    reviews, stats = ymod.build_records(records, unescape_text=True)
    orgs = ymod.group_organizations(reviews)
    return orgs, reviews, stats


def test_loads_through_src_path(tmp_path):
    path = tmp_path / "geo.tskv"
    path.write_text(render_tskv(), encoding="utf-8")
    records = ymod.load_tabular(path, "tskv")
    assert len(records) == 119
    assert records[0]["text"] == "Солнце отзыв 0\\nвторая строка"


def test_tskv_unescape():
    assert ymod._tskv_unescape("a\\nb") == "a\nb"
    assert ymod._tskv_unescape("a\\tb") == "a\tb"
    assert ymod._tskv_unescape("a\\\\b") == "a\\b"
    assert ymod._tskv_unescape("plain") == "plain"


def test_build_records_counts(organizations):
    _, reviews, stats = organizations
    assert stats.total == 119
    assert stats.usable == 116
    assert stats.skipped_rating == 2
    assert stats.skipped_no_text == 1
    assert stats.skipped_no_name == 0
    assert len(reviews) == 116


def test_classify_rubrics_known_cases():
    assert ymod.classify_rubrics("Ресторан") == "restaurant_cafe"
    assert ymod.classify_rubrics("Кафе") == "restaurant_cafe"
    assert ymod.classify_rubrics("Бар;Ночной клуб") is None
    assert ymod.classify_rubrics("Гостиница") == "hotel"
    assert ymod.classify_rubrics("Парк-отель") == "hotel"
    assert ymod.classify_rubrics("Бронирование гостиниц") is None
    assert ymod.classify_rubrics("Аптека") == "pharmacy"
    assert ymod.classify_rubrics("Ветеринарная аптека") is None
    assert ymod.classify_rubrics("Торговый центр") == "shopping_center"
    assert ymod.classify_rubrics("Торговый центр;Развлекательный центр") == "shopping_center"
    assert ymod.classify_rubrics("Гостиница;Ресторан") is None


def test_organization_category_inference(organizations):
    orgs, _, _ = organizations
    by_name = {org.name: org for org in orgs}
    assert by_name["Ресторан Солнце"].category == "restaurant_cafe"
    assert by_name["Кафе Утро"].category == "restaurant_cafe"
    assert by_name["Гостиница Центр"].category == "hotel"
    assert by_name["Аптека 24"].category == "pharmacy"
    assert by_name["ТЦ Галерея"].category == "shopping_center"
    assert by_name["ТЦ Сатурн"].category == "shopping_center"
    # reviews that match no single category do not form an organization at all
    assert "Бар Ночь" not in by_name
    assert "Бронирование гостиниц Сервис" not in by_name
    assert "Ветаптека Плюс" not in by_name
    assert "Отель и ресторан" not in by_name


def test_fleet_selection_and_sampling_deterministic(organizations):
    orgs, _, _ = organizations
    fleet = ymod.build_fleet("hotel", orgs, target=20, min_per_org=3)
    assert [org.name for org in fleet.organizations] == ["Гостиница Центр", "Парк-Отель Юг"]
    assert fleet.cohort_stats()["pool_size"] == 25
    ymod.sample_fleet(fleet, seed=42)
    assert len(fleet.sampled) == 20

    fleet_again = ymod.build_fleet("hotel", orgs, target=20, min_per_org=3)
    ymod.sample_fleet(fleet_again, seed=42)
    first = ymod.make_ids([r for r in fleet.sampled])
    second = ymod.make_ids([r for r in fleet_again.sampled])
    assert [first[r.ordinal]["review_id"] for r in fleet.sampled] == [
        second[r.ordinal]["review_id"] for r in fleet_again.sampled
    ]

    fleet_other = ymod.build_fleet("hotel", orgs, target=20, min_per_org=3)
    ymod.sample_fleet(fleet_other, seed=7)
    assert [r.ordinal for r in fleet.sampled] != [r.ordinal for r in fleet_other.sampled]


def test_shopping_center_sampling_is_balanced(organizations):
    orgs, _, _ = organizations
    fleet = ymod.build_fleet("shopping_center", orgs, target=20, min_per_org=3)
    assert [org.name for org in fleet.organizations] == ["ТЦ Галерея", "ТЦ Сатурн"]
    ymod.sample_fleet(fleet, seed=42)
    assert len(fleet.sampled) == 20
    per_place = {
        name: sum(1 for r in fleet.sampled if r.name == name)
        for name in ("ТЦ Галерея", "ТЦ Сатурн")
    }
    assert per_place == {"ТЦ Галерея": 12, "ТЦ Сатурн": 8}  # proportional allocation
    # neither place may dominate: cohort sizes stay approximately balanced
    assert abs(per_place["ТЦ Галерея"] - per_place["ТЦ Сатурн"]) <= 4


def test_min_per_org_floor_excludes_tiny_orgs(organizations):
    orgs, _, _ = organizations
    # floor 8 leaves only "Аптека 24" (8 usable) — below the 20 target, so it must raise
    with pytest.raises(ymod.NotEnoughReviewsError):
        ymod.build_fleet("pharmacy", orgs, target=20, min_per_org=8)
    # floor 3 leaves Зеленая аптека (7) and Ютека Онлайн (5) out; pool stays valid with floor 2
    fleet = ymod.build_fleet("pharmacy", orgs, target=20, min_per_org=2)
    assert [org.name for org in fleet.organizations] == [
        "Аптека 24",
        "Зеленая аптека",
        "Ютека Онлайн",
    ]


def test_fleet_not_enough_reviews_raises(organizations):
    orgs, _, _ = organizations
    with pytest.raises(ymod.NotEnoughReviewsError):
        ymod.build_fleet("pharmacy", orgs, target=1000, min_per_org=3)


def test_ids_are_stable_and_distinct():
    text = render_tskv()
    records = ymod.load_tabular_text(text, "tskv")
    reviews, _ = ymod.build_records(records, unescape_text=True)
    orgs = ymod.group_organizations(reviews)
    fleet = ymod.build_fleet("hotel", orgs, target=20, min_per_org=3)
    ymod.sample_fleet(fleet, seed=42)
    ids = ymod.make_ids(fleet.sampled)
    review_ids = [ids[r.ordinal]["review_id"] for r in fleet.sampled]
    assert len(set(review_ids)) == len(review_ids)


def test_organizations_never_merged_into_one_place_id():
    text = render_tskv()
    records = ymod.load_tabular_text(text, "tskv")
    reviews, _ = ymod.build_records(records, unescape_text=True)
    orgs = ymod.group_organizations(reviews)
    pharmacy = {org.name for org in orgs if org.category == "pharmacy"}
    assert pharmacy == {"Аптека 24", "Зеленая аптека", "Ютека Онлайн"}
    fleet = ymod.build_fleet("pharmacy", orgs, target=20, min_per_org=3)
    ymod.sample_fleet(fleet, seed=42)
    ids = ymod.make_ids(fleet.sampled)
    place_ids = {ids[r.ordinal]["place_id"] for r in fleet.sampled}
    assert len(place_ids) == 3  # one distinct place_id per (name, address)


def test_full_pipeline_writes_expected_corpus(tmp_path):
    tskv = tmp_path / "geo.tskv"
    tskv.write_text(render_tskv(), encoding="utf-8")
    out = tmp_path / "out"
    code = ymod.main(
        [
            str(tskv),
            "--out-dir",
            str(out),
            "--target-per-category",
            "20",
            "--min-per-org",
            "3",
            "--seed",
            "42",
        ]
    )
    assert code == 0

    csv_path = out / "real_reviews.csv"
    manifest_path = out / "corpus_manifest.json"
    assert csv_path.is_file()
    assert manifest_path.is_file()

    result = CSVAdapter().load(csv_path)
    assert result.report.valid == 60
    assert result.report.skipped == 0
    assert len(result.reviews) == 60

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["counts"]["total"] == 60
    assert manifest["counts"]["per_category"] == {
        "restaurant_cafe": 20,
        "hotel": 20,
        "shopping_center": 20,
    }
    assert manifest["suitable_for"] == ["templated detection", "specificity", "duplicate detection"]
    assert "reviewer history validation" in manifest["not_suitable_for"]
    assert manifest["id_generation"]["place_id"].startswith("yg23-")
    assert manifest["seed"] == 42
    # shopping_center carries its source rubric and balanced per-place allocation
    assert manifest["categories"]["shopping_center"]["source_rubrics"] == ["Торговый центр"]
    assert set(manifest["categories"]["shopping_center"]["per_org_counts"]) == {14, 10}
    # pharmacy is a reference category only, never part of the corpus
    assert "pharmacy" in manifest["reference_categories"]
    assert manifest["reference_categories"]["pharmacy"]["included_in_corpus"] is False
    assert set(manifest["counts"]["per_category"]) == {
        "restaurant_cafe",
        "hotel",
        "shopping_center",
    }


def test_full_pipeline_deterministic_across_runs(tmp_path):
    tskv = tmp_path / "geo.tskv"
    tskv.write_text(render_tskv(), encoding="utf-8")
    run_a = tmp_path / "a"
    run_b = tmp_path / "b"
    ymod.main([str(tskv), "--out-dir", str(run_a), "--target-per-category", "20", "--seed", "42"])
    ymod.main([str(tskv), "--out-dir", str(run_b), "--target-per-category", "20", "--seed", "42"])
    assert (run_a / "real_reviews.csv").read_bytes() == (run_b / "real_reviews.csv").read_bytes()


def test_text_newline_unescaped_in_corpus(tmp_path):
    text = render_tskv()
    records = ymod.load_tabular_text(text, "tskv")
    reviews, _ = ymod.build_records(records, unescape_text=True)
    assert any("\nвторая строка" in r.text for r in reviews)
    one = next(r for r in reviews if "вторая строка" in r.text)
    assert one.text == "Солнце отзыв 0\nвторая строка"


def test_parquet_and_csv_mirrors_load(tmp_path):
    text = render_tskv()
    records = ymod.load_tabular_text(text, "tskv")
    frame = pd.DataFrame.from_records(records)
    parquet = tmp_path / "geo.parquet"
    frame.to_parquet(parquet, index=False)
    loaded = ymod.load_tabular(parquet, "parquet")
    assert len(loaded) == len(records) == 119
    assert all(isinstance(r["rating"], str) for r in loaded)

    csv_path = tmp_path / "geo.csv"
    frame.rename(columns={"name_ru": "name", "rubrics": "category"}).to_csv(csv_path, index=False)
    loaded_csv = ymod.load_tabular(csv_path, "csv")
    assert len(loaded_csv) == 119
    assert set(loaded_csv[0]) == {"address", "name_ru", "rubrics", "rating", "text"}
