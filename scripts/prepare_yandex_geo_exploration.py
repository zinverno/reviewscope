#!/usr/bin/env python3
"""Build the ReviewScope exploratory corpus from the official Yandex Geo
Reviews Dataset 2023 (TSKV release, https://github.com/yandex/geo-reviews-dataset-2023).

This is a LARGE exploratory corpus for the ReviewScope UI — it is NOT a
validation dataset. It is generated from the already-downloaded official
parquet mirror of the 500,000-row release (columns: address, name_ru, rating,
rubrics, text).

Selection rules (documented in the manifest):

* usable review = non-empty text + rating in 1..5 + non-empty name/address;
* every real organization stays its own place_id — organizations are grouped
  by exact (name, address, rubric category) and NEVER merged across branches;
* an organization joins a category only through its own reviews' rubrics;
* per-category floor: 35 usable reviews per organization (relaxed from the
  preferred 50 because the release's per-place cohorts are small — the largest
  cohort anywhere has 226 usable reviews);
* ALL usable reviews of every selected organization are kept — nothing is
  sampled or rebalanced, so rating distributions and cohort sizes are natural;
* row order is deterministic (usable count DESC, then name/address), so the
  whole selection is a pure function of the source file and the seed constant.

Outputs go to ``validation_data/private/yandex_geo_2023/exploration/``
(``exploration_reviews.csv`` and ``exploration_manifest.json``) and are
gitignored.

Example::

    python scripts/prepare_yandex_geo_exploration.py \\
        ~/datasets/yandex-geo-reviews-2023/yandex_geo_reviews_2023.parquet
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.ingestion.csv_adapter import CSVAdapter  # noqa: E402

CORPUS_NAME = "yandex_geo_2023_exploration"
DEFAULT_SEED = 20260901
MIN_PER_ORG = 35

SOURCE_LABEL = "yandex_geo_2023_exploration"

OUT_SUBDIR = Path("validation_data/private/yandex_geo_2023/exploration")
CSV_FILE = "exploration_reviews.csv"
MANIFEST_FILE = "exploration_manifest.json"

CSV_COLUMNS = [
    "review_id",
    "place_id",
    "place_name",
    "place_category",
    "reviewer_id",
    "reviewer_name",
    "rating",
    "text",
    "published_at",
    "city",
    "region",
    "country",
    "latitude",
    "longitude",
    "source",
    "source_url",
]

# Category matching over the semicolon-separated Russian ``rubrics`` column.
# A review joins the category when at least one positive keyword matches a
# rubric token and no negative keyword does; the organization joins the
# category of its own reviews. Reviews whose rubrics match several target
# categories at once are excluded (ambiguous).
_CATEGORY_DEFS: dict[str, dict[str, tuple[str, ...]]] = {
    "restaurant_cafe": {
        "positive": (
            "ресторан",
            "кафе",
            "кофейн",
            "столов",
            "быстрое питание",
            "пиццер",
            "суши",
            "шаурм",
            "бургер",
            "пекарн",
            "кондитер",
            "бистро",
            "трактир",
            "закусочн",
            "чебуречн",
        ),
        "negative": ("бар, паб", "кальян", "караоке", "ночной клуб", "стрип"),
    },
    "hotel": {
        "positive": (
            "гостиниц",
            "отель",
            "хостел",
            "мотель",
            "гостевой дом",
            "пансионат",
            "апарт",
        ),
        "negative": (
            "бронирован",
            "агентств",
            "недвижим",
            "сайт",
            "онлайн",
            "суточно",
            "информационн",
            "услуг",
            "для животных",
            "ветеринарн",
        ),
    },
    "shopping_center": {
        "positive": ("торговый центр",),
        "negative": (),
    },
    "museum": {
        "positive": ("музей",),
        "negative": (),
    },
    "park": {
        "positive": (
            "парк культуры и отдыха",
            "парк аттракционов",
            "лесопарк",
            "верёвочный парк",
            "городской парк",
            "парк имени",
            "парк",
        ),
        "negative": (
            "аквапарк",
            "зоопарк",
            "автобусный",
            "таксопарк",
            "технопарк",
            "паркет",
            "парковк",
            "паркомат",
            "скейт-парк",
            "велопарковк",
            "троллейбусн",
            "парк-отель",
            "зоомагазин",
            "автомобильн",
        ),
    },
    "zoo": {
        "positive": ("зоопарк",),
        "negative": ("зоомагазин", "зоосалон", "зооцентр", "зоопарикмахер", "зоогостиниц"),
    },
    "aquapark": {
        "positive": ("аквапарк",),
        "negative": ("аквариум", "строительство и монтаж"),
    },
    "beauty_salon": {
        "positive": (
            "салон красоты",
            "парикмахер",
            "барбершоп",
            "ногтевая студия",
            "косметологи",
            "салон бровей и ресниц",
            "эпиляция",
            "спа-салон",
            "массажный салон",
            "тату-салон",
            "пирсинг-салон",
            "бьюти",
        ),
        "negative": (
            "зоосалон",
            "оборудование",
            "обучение мастеров",
            "магазин",
            "салон оптики",
            "салон связи",
            "салон вечерней одежды",
            "свадебный салон",
            "художественный салон",
            "эротический массаж",
            "авто",
            "мото",
            "ортопедический",
            "одежд",
        ),
    },
    "medical_clinic": {
        "positive": (
            "клиник",
            "поликлиник",
            "медцентр",
            "медицинский центр",
            "медицинск",
            "стоматолог",
            "больниц",
            "диагностический центр",
            "медицинская лаборатория",
            "наркологическ",
        ),
        "negative": (
            "ветеринарн",
            "для животных",
            "магазин медицинских товаров",
            "медицинские изделия",
            "медицинское оборудование",
            "стоматологические материалы",
            "скорая медицинская помощь",
            "медицинская помощь на дому",
            "медицинская комиссия",
            "медицинская мебель",
            "медицинский туризм",
            "судебно",
        ),
    },
    "fitness_club": {
        "positive": (
            "фитнес-клуб",
            "фитнес",
            "спортивный, тренажёрный зал",
            "спортивный зал",
            "тренажёрн",
            "тренажерн",
            "спортивный клуб, секция",
            "спортивный комплекс",
            "бассейн",
            "йога",
        ),
        "negative": (
            "магазин",
            "одежд",
            "питание",
            "инвентарь",
            "киберспорт",
            "спортбар",
            "школа",
            "касса",
            "площадк",
            "стадион",
            "ипподром",
            "объединение",
            "строительство",
            "продажа бассейнов",
            "обслуживание бассейнов",
            "для животных",
            "развлекательн",
            "танцевальн",
            "спортивная база",
            "спортивный центр",
            "спортивно-развлекательн",
        ),
    },
}

SOURCE_RUBRICS = {
    "restaurant_cafe": "Кафе; Ресторан; Кофейня; Столовая; Быстрое питание; Пиццерия; Суши-бар",
    "hotel": "Гостиница; Хостел; Пансионат",
    "shopping_center": "Торговый центр",
    "museum": "Музей",
    "park": "Парк культуры и отдыха; Парк аттракционов; Лесопарк",
    "zoo": "Зоопарк",
    "aquapark": "Аквапарк",
    "beauty_salon": "Салон красоты; Парикмахерская; Барбершоп; Ногтевая студия; Косметология; Спа-салон",
    "medical_clinic": "Медцентр, клиника; Стоматологическая клиника; Поликлиника; Больница; Диагностический центр",
    "fitness_club": "Фитнес-клуб; Спортивный, тренажёрный зал; Бассейн",
}


@dataclass
class RawReview:
    name: str
    address: str
    rubrics: str
    primary_rubric: str
    rating: int
    text: str
    ordinal: int
    category: str | None


@dataclass
class Organization:
    key: tuple[str, str, str]
    name: str
    address: str
    rubrics: str
    primary_rubric: str
    category: str
    reviews: list[RawReview] = field(default_factory=list)

    @property
    def usable_count(self) -> int:
        return len(self.reviews)


def classify_rubrics(rubrics: str) -> str | None:
    """Return the single target category a review belongs to, else None."""
    tokens = [tok.strip().lower() for tok in rubrics.split(";") if tok.strip()]
    if not tokens:
        return None
    matched: list[str] = []
    for category, rules in _CATEGORY_DEFS.items():
        positive = any(any(kw in tok for kw in rules["positive"]) for tok in tokens)
        negative = any(any(kw in tok for kw in rules["negative"]) for tok in tokens)
        if positive and not negative:
            matched.append(category)
    return matched[0] if len(matched) == 1 else None


def build_reviews(df: pd.DataFrame) -> list[RawReview]:
    reviews: list[RawReview] = []
    for ordinal, row in enumerate(df.itertuples(index=False)):
        text = str(row.text).strip()
        rating = int(row.rating)
        name = str(row.name_ru).strip()
        address = str(row.address).strip()
        rubrics = str(row.rubrics).strip() or ""
        category = classify_rubrics(rubrics)
        tokens = [tok.strip() for tok in rubrics.split(";") if tok.strip()]
        reviews.append(
            RawReview(
                name=name,
                address=address,
                rubrics=rubrics,
                primary_rubric=tokens[0] if tokens else "",
                rating=rating,
                text=text,
                ordinal=ordinal,
                category=category,
            )
        )
    return reviews


def group_organizations(reviews: list[RawReview]) -> list[Organization]:
    by_key: dict[tuple[str, str, str], Organization] = {}
    for review in reviews:
        if review.category is None:
            continue
        key = (review.name, review.address, review.category)
        org = by_key.get(key)
        if org is None:
            org = Organization(
                key=key,
                name=review.name,
                address=review.address,
                rubrics=review.rubrics,
                primary_rubric=review.primary_rubric,
                category=review.category,
            )
            by_key[key] = org
        org.reviews.append(review)
    return list(by_key.values())


def select_organizations(organizations: list[Organization]) -> tuple[list[Organization], dict[str, int]]:
    """Deterministic selection: usable_count DESC, then name/address.

    No sampling: every usable review of every selected organization is kept.
    """
    eligible = [org for org in organizations if org.usable_count >= MIN_PER_ORG]
    eligible.sort(key=lambda org: (-org.usable_count, org.name.lower(), org.address.lower()))
    return eligible, dict(Counter(org.usable_count >= 50 for org in eligible))


def _sha256(*parts: str, length: int) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def place_id_for(name: str, address: str, category: str) -> str:
    """Stable local place id: one id per (name, address, category) triple.

    Distinct physical organizations (branches included) never share an id.
    """
    return "yg23x-" + _sha256(name, address, category, length=16)


def build_csv_rows(organizations: list[Organization]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for org in organizations:
        place_id = place_id_for(org.name, org.address, org.category)
        for review in org.reviews:
            review_id = "yg23x-" + _sha256(
                place_id, str(review.rating), str(review.ordinal), review.text, length=24
            )
            reviewer_id = "anonx-" + _sha256(review_id, length=12)
            rows.append(
                {
                    "review_id": review_id,
                    "place_id": place_id,
                    "place_name": review.name,
                    "place_category": review.primary_rubric,
                    "reviewer_id": reviewer_id,
                    "reviewer_name": "",
                    "rating": str(review.rating),
                    "text": review.text,
                    "published_at": "",
                    "city": "",
                    "region": "",
                    "country": "",
                    "latitude": "",
                    "longitude": "",
                    "source": SOURCE_LABEL,
                    "source_url": "",
                }
            )
    rows.sort(key=lambda row: (row["place_id"], row["review_id"]))
    return rows


def rating_distribution(reviews: list[RawReview]) -> dict[str, int]:
    return {str(star): sum(1 for r in reviews if r.rating == star) for star in range(1, 6)}


def quantiles(values: list[int]) -> dict[str, float | int]:
    if not values:
        return {"min": 0, "p25": 0, "median": 0, "p75": 0, "max": 0}
    arr = np.array(sorted(values), dtype=float)
    return {
        "min": int(arr.min()),
        "p25": round(float(np.percentile(arr, 25)), 1),
        "median": round(float(np.percentile(arr, 50)), 1),
        "p75": round(float(np.percentile(arr, 75)), 1),
        "max": int(arr.max()),
    }


def build_manifest(
    *,
    source_path: Path,
    total_rows: int,
    usable_rows: int,
    category_rows: int,
    reviews: list[RawReview],
    organizations: list[Organization],
    selected: list[Organization],
    rows: list[dict[str, str]],
    seed: int,
    min_per_org: int,
    out_dir: Path,
) -> dict[str, Any]:
    all_sampled = [r for org in selected for r in org.reviews]
    by_cat_selected: dict[str, list[Organization]] = defaultdict(list)
    for org in selected:
        by_cat_selected[org.category].append(org)

    categories_blob: dict[str, Any] = {}
    for category in _CATEGORY_DEFS:
        orgs = by_cat_selected.get(category, [])
        cohort_counts = sorted(org.usable_count for org in orgs)
        cohort_reviews = [r for org in orgs for r in org.reviews]
        categories_blob[category] = {
            "source_rubric": SOURCE_RUBRICS[category],
            "organizations": len(orgs),
            "reviews": len(cohort_reviews),
            "per_org_review_counts": [
                {"name_ru": org.name, "address": org.address, "count": org.usable_count}
                for org in sorted(orgs, key=lambda o: (-o.usable_count, o.name.lower()))
            ],
            "per_org_quantiles": quantiles(cohort_counts),
            "organizations_ge_50": sum(1 for c in cohort_counts if c >= 50),
            "organizations_ge_100": sum(1 for c in cohort_counts if c >= 100),
            "rating_distribution": rating_distribution(cohort_reviews),
            "source_rubrics_primary": sorted({r.primary_rubric for r in cohort_reviews}),
        }

    overall_counts = [org.usable_count for org in selected]
    place_id_counts = Counter(row["place_id"] for row in rows)
    assert len(place_id_counts) == len(selected)

    return {
        "corpus_name": CORPUS_NAME,
        "purpose": (
            "Large REAL exploratory corpus for the ReviewScope UI (Overview / Topics / "
            "Anomalies / Duplicates / Reviewers / Reviewed Places / Data Quality pages). "
            "This is NOT a validation dataset and must not be used to measure detector "
            "accuracy."
        ),
        "source_dataset": {
            "name": "Yandex Geo Reviews Dataset 2023",
            "official_repo": "https://github.com/yandex/geo-reviews-dataset-2023",
            "license": "MIT",
            "source_path_used": str(source_path),
            "source_format": "parquet",
            "source_columns": ["address", "name_ru", "rubrics", "rating", "text"],
            "rows_loaded": total_rows,
            "rows_usable": usable_rows,
            "rows_with_target_category": category_rows,
            "rows_skipped": {
                "no_rating_1_5_or_no_text_or_no_name_address": total_rows - usable_rows,
                "category_ambiguous_or_other": usable_rows - category_rows,
            },
        },
        "categories": categories_blob,
        "counts": {
            "total_organizations": len(selected),
            "total_reviews": len(all_sampled),
            "per_category_organizations": {
                category: len(by_cat_selected[category]) for category in _CATEGORY_DEFS
            },
            "per_category_reviews": {
                category: sum(org.usable_count for org in by_cat_selected[category])
                for category in _CATEGORY_DEFS
            },
            "per_org_reviews": quantiles(overall_counts),
        },
        "rating_distributions": {
            category: rating_distribution([r for org in by_cat_selected.get(category, []) for r in org.reviews])
            for category in _CATEGORY_DEFS
        }
        | {"overall": rating_distribution(all_sampled)},
        "selection": {
            "min_per_org": min_per_org,
            "rule": (
                "Every organization with >= min_per_org usable reviews is selected; ALL of "
                "its usable reviews are kept. Usable = non-empty text, rating 1..5, non-empty "
                "name and address. Organizations are ranked by usable count only to organise "
                "the manifest; nothing is sampled or rebalanced, so rating distributions and "
                "cohort sizes are the natural ones. Branches and distinct organizations are "
                "never merged: place_id is a stable hash of (name, address, category)."
            ),
            "rationale": (
                "The release's per-place cohorts are small (the largest usable cohort is 226 "
                "reviews). The preferred floor of 50 was relaxed to 35 so a ~10,000-review "
                "corpus stays buildable from real cohorts; organizations with 50+ and 100+ "
                "usable reviews are reported per category so the stricter preference can be "
                "sub-selected later if desired."
            ),
            "seed": seed,
            "deterministic": True,
        },
        "schema_conversion": {
            "target_schema": "ReviewScope NormalizedReview CSV ingestion schema",
            "column_mapping": {
                "place_name": "name_ru",
                "place_category": "primary rubric of the organization",
                "rating": "rating",
                "text": "text",
                "source": 'constant "yandex_geo_2023_exploration"',
                "review_id/place_id/reviewer_id": "stable local ids (see id_generation)",
            },
            "unavailable_source_fields": {
                "reviewer_id": "the release carries no reviewer identity; reviewer_id is a "
                "synthetic per-review opaque token",
                "reviewer_name": "unavailable",
                "published_at": "the release carries no per-review timestamp",
                "city": "unavailable",
                "region": "unavailable",
                "country": "unavailable",
                "latitude": "unavailable",
                "longitude": "unavailable",
                "source_url": "unavailable",
            },
        },
        "id_generation": {
            "place_id": "yg23x-<sha256(name + address + category)[:16]> — one id per "
            "(name, address, category); distinct physical organizations are never merged",
            "review_id": "yg23x-<sha256(place_id | rating | ordinal | text)[:24]> — stable across "
            "runs; ordinal is the row position in the source parquet",
            "reviewer_id": "anonx-<sha256(review_id)[:12]> — synthetic per-review opaque token; "
            "every review belongs to a distinct synthetic reviewer, real user identity is NOT "
            "invented or reconstructed",
        },
        "suitable_for": [
            "templated detection",
            "specificity",
            "duplicate detection",
            "topic / keyword exploration",
            "coordinated-activity exploration (with the caveats below)",
        ],
        "not_suitable_for": [
            "reviewer-history analytics",
            "local-familiarity validation",
            "temporal-burst validation",
            "travel-history analysis",
        ],
        "analytical_limitations": [
            "Reviewer identity is absent from the source: reviewer_id is a synthetic per-review "
            "token, so every review looks like a distinct reviewer. Reviewer-history, "
            "local-familiarity and travel-history signals are degenerate by construction.",
            "Per-review timestamps are absent: published_at is empty for every row. Volume-burst, "
            "rating-anomaly-window, recency and emerging-keyword signals cannot run and the "
            "coordinated-activity score loses its temporal components.",
            "The coordinated-activity score still runs, but only its non-temporal components "
            "(duplicate density, template similarity, reviewer overlap) are meaningful; any "
            "elevated score can only come from textual/duplicate structure, not from review "
            "timing.",
            "Per-place cohorts are small (35-226 usable reviews), so within-place "
            "templated/duplicate structure and peer-similarity signals are weaker than on "
            "larger cohorts.",
            "Rating distributions are preserved exactly as released, including the source's "
            "strong 5-star skew (the release has no per-place rater balancing).",
            "An organization joins a category only through the rubrics of its own reviews, so an "
            "organization whose reviews carry mixed rubrics may contribute fewer reviews than "
            "its full review count.",
            "This corpus is NOT a validation dataset: it carries no labels and must not be used "
            "to measure detector accuracy or to tune thresholds.",
        ],
        "outputs": {
            "csv": str(out_dir / CSV_FILE),
            "manifest": str(out_dir / MANIFEST_FILE),
        },
        "generated_at_utc": _dt.datetime.now(_dt.UTC).isoformat(),
    }


def verify_corpus(csv_path: Path, expected: int) -> None:
    print(f"\nVerifying corpus through the production CSVAdapter: {csv_path}")
    result = CSVAdapter().load(csv_path)
    print(f"  rows imported: {result.report.total_rows}")
    print(f"  rows valid: {result.report.valid}")
    print(f"  rows skipped: {result.report.skipped}")
    print(f"  warnings: {result.report.warning_count}")
    if result.report.warnings:
        for warning in result.report.warnings[:20]:
            print(f"    [warning] {warning}")
    nullable_counts = Counter()
    for review in result.reviews:
        for column in (
            "reviewer_name",
            "published_at",
            "city",
            "region",
            "country",
            "latitude",
            "longitude",
            "source_url",
        ):
            if getattr(review, column) is None:
                nullable_counts[column] += 1
    for column in (
        "reviewer_name",
        "published_at",
        "city",
        "region",
        "country",
        "latitude",
        "longitude",
        "source_url",
    ):
        print(f"    {column}: {nullable_counts.get(column, 0)} None / {len(result.reviews)} rows")
    if len(result.reviews) != expected:
        raise AssertionError(f"expected exactly {expected} reviews, got {len(result.reviews)}")
    if result.report.skipped:
        raise AssertionError(f"expected zero skipped rows, got {result.report.skipped}")
    print(f"  ASSERT ok: exactly {expected} reviews loaded with zero skipped rows")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the ReviewScope exploratory corpus from the official "
        "Yandex Geo Reviews Dataset 2023."
    )
    parser.add_argument(
        "dataset", help="Path to the official parquet download (mirror of the 2023 release)."
    )
    parser.add_argument("--out-dir", default=str(OUT_SUBDIR), help="Output directory.")
    parser.add_argument("--min-per-org", type=int, default=MIN_PER_ORG)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    source_path = Path(args.dataset)
    if not source_path.is_file():
        parser.error(f"dataset file not found: {source_path}")

    print(f"Loading {source_path} (format: parquet) ...")
    df = pd.read_parquet(source_path, engine="pyarrow")
    df = df.astype(str)
    print(f"  rows loaded: {len(df):,}")

    df["_text_ok"] = df["text"].str.strip().ne("")
    df["_rating_ok"] = df["rating"].astype(int).between(1, 5)
    df["_name_ok"] = df["name_ru"].str.strip().ne("")
    df["_addr_ok"] = df["address"].str.strip().ne("")
    usable = df[df["_text_ok"] & df["_rating_ok"] & df["_name_ok"] & df["_addr_ok"]]
    print(
        f"  usable reviews (non-empty text, rating 1..5, name+address): "
        f"{len(usable):,}"
    )

    reviews = build_reviews(usable)
    organizations = group_organizations(reviews)
    print(f"  usable target-category reviews: {sum(1 for r in reviews if r.category is not None):,}")
    print(f"  organizations (name, address, category): {len(organizations):,}")

    per_category = {
        category: sum(1 for org in organizations if org.category == category) for category in _CATEGORY_DEFS
    }
    print(f"  candidate organizations by category: {per_category}")

    for category in _CATEGORY_DEFS:
        counts = sorted(
            org.usable_count for org in organizations if org.category == category
        )
        if not counts:
            continue
        print(
            f"    {category}: max cohort {counts[-1]}, "
            f">= {args.min_per_org}: "
            f"{sum(1 for c in counts if c >= args.min_per_org)} orgs, "
            f">= 50: {sum(1 for c in counts if c >= 50)}, "
            f">= 100: {sum(1 for c in counts if c >= 100)}"
        )

    selected, tiers = select_organizations(organizations)
    print(
        f"\nSelected organizations (>= {args.min_per_org} usable reviews): "
        f"{len(selected)} ({tiers[True]} with 50+, {tiers[False]} with < 50)"
    )
    total_reviews = sum(org.usable_count for org in selected)
    print(f"Total reviews in corpus: {total_reviews:,}")

    rows = build_csv_rows(selected)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / CSV_FILE
    manifest_path = out_dir / MANIFEST_FILE

    frame = pd.DataFrame.from_records(rows, columns=CSV_COLUMNS)
    frame.to_csv(csv_path, index=False, encoding="utf-8")

    manifest = build_manifest(
        source_path=source_path,
        total_rows=len(df),
        usable_rows=len(usable),
        category_rows=sum(1 for r in reviews if r.category is not None),
        reviews=reviews,
        organizations=organizations,
        selected=selected,
        rows=rows,
        seed=args.seed,
        min_per_org=args.min_per_org,
        out_dir=out_dir,
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\nWrote:")
    print(f"  {csv_path}")
    print(f"  {manifest_path}")

    verify_corpus(csv_path, expected=len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
