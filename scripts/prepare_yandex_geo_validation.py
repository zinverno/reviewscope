#!/usr/bin/env python3
"""Prepare the first ReviewScope real-world validation corpus.

Converter for the official **Yandex Geo Reviews Dataset 2023** (TSKV release,
https://github.com/yandex/geo-reviews-dataset-2023). The script is tolerant to
the release's real-world distributions:

* input is auto-detected — official ``*.tskv`` (optionally gzip/bz2), the
  identical-columns community parquet mirror, or the "native csv" mirror
  (columns ``address``, ``name_ru``, ``rubrics``, ``rating``, ``text``);
* usable reviews are non-empty-text rows with a 1..5 rating;
* organizations are grouped by exact (name, address, rubric category) so a
  review only joins the category its own rubrics match; branches are never
  merged into one place;
* for every target category a deterministic top-K *fleet* (largest usable
  counts first, ``--min-per-org`` floor) is pooled until it holds
  ``--target-per-category`` usable reviews, then exactly that many reviews are
  sampled per category with a seeded RNG;
* the sampled reviews are projected onto the ReviewScope CSV ingestion schema
  with stable local ids and every unavailable field left nullable.

The official dataset carries **no reviewer identity and no per-review
timestamp**, so the corpus is only suitable for templated, specificity and
duplicate detection. Reviewer-history, local-familiarity, temporal-burst and
travel-history validation are marked NOT suitable in the manifest.

Example::

    python scripts/prepare_yandex_geo_validation.py \\
        ~/datasets/yandex-geo-reviews-2023/yandex_geo_reviews_2023.parquet

Outputs go to ``validation_data/private/yandex_geo_2023/`` (``real_reviews.csv``
and ``corpus_manifest.json``) and are gitignored.
"""

from __future__ import annotations

import argparse
import bz2
import datetime as _dt
import gzip
import hashlib
import io
import json
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from reviewscope.ingestion.csv_adapter import CSVAdapter  # noqa: E402

CORPUS_NAME = "yandex_geo_2023"
DEFAULT_SEED = 20260901
DEFAULT_TARGET_PER_CATEGORY = 120
DEFAULT_MIN_PER_ORG = 3

TARGET_CATEGORIES = ("restaurant_cafe", "hotel", "shopping_center")

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

# Canonical place for review texts, mirroring the NormalizedReview contract.
SOURCE_LABEL = "yandex_geo_2023"

# Category matching over the semicolon-separated Russian ``rubrics`` column.
_CATEGORY_DEFS: dict[str, dict[str, tuple[str, ...]]] = {
    "restaurant_cafe": {
        # food-service venues
        "positive": (
            "ресторан",
            "кафе",
            "кофейн",
            "кофе",
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
        # pure drink / nightlife venues are not a restaurant/cafe
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
        # accommodation *booking/brokerage* rubrics are not hotels themselves
        "negative": (
            "бронирован",
            "агентств",
            "недвижим",
            "сайт",
            "онлайн",
            "суточно",
            "информационн",
            "услуг",
        ),
    },
    # reference-only category: ``pharmacy`` is recognized so its feasibility
    # statistics stay computable, but it is excluded from TARGET_CATEGORIES and
    # therefore from the evaluation corpus.
    "pharmacy": {
        "positive": ("аптек",),
        "negative": ("ветеринарн", "зоо", "ветклиник"),
    },
    "shopping_center": {
        "positive": ("торговый центр",),
        "negative": (),
    },
}

# Categories kept for feasibility statistics only; never written to the corpus.
_REFERENCE_CATEGORIES = ("pharmacy",)

# Human-readable source rubric label per reference category (manifest notes).
_REFERENCE_SOURCE_RUBRICS = {"pharmacy": "Аптека"}

# Column alias table for the "native csv" mirror (keys are the expected ones).
_CSV_ALIASES = {
    "name_ru": ("name_ru", "name", "org_name", "organization_name", "place_name"),
    "address": ("address", "addr", "place_address"),
    "rubrics": ("rubrics", "rubric", "category", "categories", "org_category"),
    "rating": ("rating", "rate", "score", "stars"),
    "text": ("text", "review", "review_text", "comment", "content"),
}

TSKV_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", "=": "="}


def _tskv_unescape(value: str) -> str:
    """Decode TSKV backslash escapes ({'\\\\n', '\\\\t', '\\\\', ...})."""
    out: list[str] = []
    i, n = 0, len(value)
    while i < n:
        char = value[i]
        if char == "\\" and i + 1 < n and value[i + 1] in TSKV_ESCAPES:
            out.append(TSKV_ESCAPES[value[i + 1]])
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _parse_tskv_line(line: str) -> dict[str, str] | None:
    """Parse one TSKV record ('tskv\\tkey=value\\t...') into a dict."""
    fields = line.split("\t")
    record: dict[str, str] = {}
    for part in fields:
        if not part:
            continue
        if "=" not in part:
            if part.strip() == "tskv":
                continue
            return None
        key, _, value = part.partition("=")
        record[key.strip()] = _tskv_unescape(value.strip())
    return record or None


def _decode_bytes(data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _load_bytes(path: Path) -> bytes:
    """Read a file, decompressing gzip/bz2 on the fly (official .tskv ships gz/bz2)."""
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    if data[:3] == b"BZh":
        return bz2.decompress(data)
    return data


def _detect_format(path: Path, declared: str) -> str:
    if declared != "auto":
        return declared
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return "parquet"
    if suffix in (".tskv", ".txt", ".log"):
        return "tskv"
    if suffix in (".csv", ".tsv"):
        return "csv"
    data = _load_bytes(path)[:4096]
    text = _decode_bytes(data)
    head = text.lstrip()[:64]
    if head.startswith("PAR1") or b"PAR1" in data[:128]:
        return "parquet"
    if "name_ru=" in head or head.startswith("tskv"):
        return "tskv"
    return "csv"


def _sniff_delimiter(text: str, candidates: tuple[str, ...] = ("\t", ",", ";", "|")) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln][:5]
    if not lines:
        return ","
    best, best_score = ",", -1
    for delim in candidates:
        score = sum(line.count(delim) for line in lines)
        if score > best_score:
            best, best_score = delim, score
    return best


def _map_csv_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename a CSV mirror's columns onto the canonical release names."""
    lower = {str(col).strip().lower(): col for col in frame.columns}
    rename: dict[str, str] = {}
    for canonical, aliases in _CSV_ALIASES.items():
        for alias in aliases:
            if alias in lower and canonical not in rename:
                rename[lower[alias]] = canonical
                break
    return frame.rename(columns=rename)


def load_tabular(path: Path, fmt: str) -> list[dict[str, Any]]:
    """Load the dataset in its downloaded form into raw release-style records."""
    if fmt == "parquet":
        frame = pd.read_parquet(path, engine="pyarrow", dtype_backend="numpy_nullable")
        frame = frame.astype(str)
        return frame.to_dict(orient="records")
    return load_tabular_text(_decode_bytes(_load_bytes(path)), fmt)


def load_tabular_text(text: str, fmt: str) -> list[dict[str, Any]]:
    """Parse raw text (TSKV or delimited) into raw release-style records."""
    if fmt == "tskv":
        records: list[dict[str, str]] = []
        for line in io.StringIO(text):
            record = _parse_tskv_line(line.rstrip("\n"))
            if record:
                records.append(record)
        return records
    delim = _sniff_delimiter(text)
    frame = pd.read_csv(io.StringIO(text), sep=delim, dtype=str, keep_default_na=False)
    frame = _map_csv_columns(frame)
    return frame.to_dict(orient="records")


def _coerce_rating(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        rating = int(float(str(value).strip().replace(",", ".")))
    except (TypeError, ValueError):
        return None
    return rating if 1 <= rating <= 5 else None


def _is_blank(value: Any) -> bool:
    if value is None:
        return True
    return not str(value).strip()


def _coerce_text(value: Any) -> str | None:
    if _is_blank(value):
        return None
    return str(value).strip()


@dataclass
class RecordStats:
    total: int = 0
    usable: int = 0
    skipped_rating: int = 0
    skipped_no_text: int = 0
    skipped_no_name: int = 0

    def skipped_total(self) -> int:
        return self.skipped_rating + self.skipped_no_text + self.skipped_no_name


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
    key: tuple[str, str]
    name: str
    address: str
    rubrics: str
    primary_rubric: str
    category: str | None
    reviews: list[RawReview] = field(default_factory=list)

    @property
    def usable_count(self) -> int:
        return len(self.reviews)


def classify_rubrics(rubrics: str) -> str | None:
    """Return the single target category an organization belongs to, else None."""
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


def build_records(
    records: list[dict[str, Any]], unescape_text: bool
) -> tuple[list[RawReview], RecordStats]:
    """Project raw release rows into usable raw reviews plus skip statistics."""
    stats = RecordStats(total=len(records))
    reviews: list[RawReview] = []
    for ordinal, record in enumerate(records):
        name = _coerce_text(record.get("name_ru"))
        address = _coerce_text(record.get("address"))
        rubrics = _coerce_text(record.get("rubrics")) or ""
        text = _coerce_text(record.get("text"))
        if unescape_text and text is not None:
            text = _tskv_unescape(text)
        rating = _coerce_rating(record.get("rating"))

        if rating is None:
            stats.skipped_rating += 1
            continue
        if text is None:
            stats.skipped_no_text += 1
            continue
        if name is None or address is None:
            stats.skipped_no_name += 1
            continue

        stats.usable += 1
        tokens = [tok.strip() for tok in rubrics.split(";") if tok.strip()]
        category = classify_rubrics(rubrics)
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
    return reviews, stats


def group_organizations(reviews: list[RawReview]) -> list[Organization]:
    """Group usable reviews by (name, address, category); each org joins one category."""
    by_key: dict[tuple[str, str, str], Organization] = {}
    for review in reviews:
        if review.category is None:
            continue
        org = by_key.get((review.name, review.address, review.category))
        if org is None:
            org = Organization(
                key=(review.name, review.address),
                name=review.name,
                address=review.address,
                rubrics=review.rubrics,
                primary_rubric=review.primary_rubric,
                category=review.category,
            )
            by_key[(review.name, review.address, review.category)] = org
        org.reviews.append(review)
    return list(by_key.values())


@dataclass
class Fleet:
    category: str
    target: int
    organizations: list[Organization]
    pool: list[RawReview]
    sampled: list[RawReview]

    @property
    def per_org_counts(self) -> list[int]:
        return [len(org.reviews) for org in self.organizations]

    def cohort_stats(self) -> dict[str, float | int]:
        counts = sorted(self.per_org_counts)
        mid = len(counts) // 2
        if len(counts) % 2 == 1:
            median = counts[mid]
        else:
            median = (counts[mid - 1] + counts[mid]) / 2
        return {
            "organizations": len(counts),
            "min_per_org": counts[0] if counts else 0,
            "median_per_org": median,
            "max_per_org": counts[-1] if counts else 0,
            "pool_size": len(self.pool),
            "sample_size": len(self.sampled),
        }


class NotEnoughReviewsError(RuntimeError):
    pass


def _org_sort_key(org: Organization) -> tuple[int, str, str]:
    return (-org.usable_count, org.name.lower(), org.address.lower())


def build_fleet(
    category: str,
    organizations: list[Organization],
    target: int,
    min_per_org: int,
) -> Fleet:
    """Pick the largest-usable-count organizations whose pooled reviews >= target."""
    eligible = [
        org for org in organizations if org.category == category and org.usable_count >= min_per_org
    ]
    eligible.sort(key=_org_sort_key)
    fleet: list[Organization] = []
    pooled = 0
    for org in eligible:
        fleet.append(org)
        pooled += org.usable_count
        if pooled >= target:
            break
    if pooled < target:
        raise NotEnoughReviewsError(
            f"category '{category}': largest {len(fleet)} organization(s) "
            f"pool only {pooled} usable reviews; need {target} "
            f"(min {min_per_org} per org)."
        )
    pool = [review for org in fleet for review in org.reviews]
    return Fleet(category=category, target=target, organizations=fleet, pool=pool, sampled=[])


def sample_fleet(fleet: Fleet, seed: int) -> None:
    """Sample exactly ``target`` reviews with approximately balanced place cohorts.

    The target is allocated across the fleet's organizations in proportion to
    each organization's usable pool (largest-remainder integer allocation), then
    a seeded RNG samples uniformly within each organization. Place cohorts stay
    roughly balanced (no single place dominates) and rating distribution is
    preserved naturally — ratings are never stratified or rebalanced.
    """
    organizations = fleet.organizations
    sizes = [len(org.reviews) for org in organizations]
    total = sum(sizes)
    exact = [fleet.target * size / total for size in sizes]
    allocation = [int(part) for part in exact]
    leftover = fleet.target - sum(allocation)
    if leftover:
        remaining = sorted(
            range(len(sizes)),
            key=lambda i: (exact[i] - allocation[i], sizes[i]),
            reverse=True,
        )
        for i in range(leftover):
            allocation[remaining[i]] += 1

    sampled: list[RawReview] = []
    for org, count in zip(organizations, allocation, strict=True):
        rng = random.Random(f"{seed}:{fleet.category}:{org.key[0]}|{org.key[1]}")
        sampled.extend(rng.sample(org.reviews, count))
    fleet.sampled = sorted(
        sampled, key=lambda review: (review.name, review.address, review.ordinal)
    )


def _sha256(*parts: str, length: int) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:length]


def place_id_for(name: str, address: str) -> str:
    """Stable local place id derived from the organization's (name, address)."""
    return "yg23-" + _sha256(name, address, length=16)


def make_ids(sampled: list[RawReview]) -> dict[int, dict[str, str]]:
    """Stable local ids: one place_id per organization, one review_id per review."""
    ids: dict[int, dict[str, str]] = {}
    for review in sampled:
        place_id = place_id_for(review.name, review.address)
        review_id = "yg23-" + _sha256(
            place_id, str(review.rating), str(review.ordinal), review.text, length=24
        )
        reviewer_id = "anon-" + _sha256(review_id, length=12)
        ids[review.ordinal] = {
            "place_id": place_id,
            "review_id": review_id,
            "reviewer_id": reviewer_id,
        }
    return ids


def build_csv_rows(
    fleets: dict[str, Fleet],
    ids: dict[int, dict[str, str]],
) -> list[dict[str, str]]:
    """Project sampled reviews onto the ReviewScope CSV ingestion schema."""
    rows: list[dict[str, str]] = []
    for fleet in fleets.values():
        for review in fleet.sampled:
            identifiers = ids[review.ordinal]
            rows.append(
                {
                    "review_id": identifiers["review_id"],
                    "place_id": identifiers["place_id"],
                    "place_name": review.name,
                    "place_category": review.primary_rubric,
                    "reviewer_id": identifiers["reviewer_id"],
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


def _org_dump(fleet: Fleet) -> list[dict[str, Any]]:
    organizations: list[dict[str, Any]] = []
    for org in sorted(fleet.organizations, key=_org_sort_key):
        corpus_reviews = [
            r for r in fleet.sampled if r.name == org.name and r.address == org.address
        ]
        organizations.append(
            {
                "place_id": place_id_for(org.name, org.address),
                "name_ru": org.name,
                "address": org.address,
                "primary_rubric": org.primary_rubric,
                "rubrics": [tok.strip() for tok in org.rubrics.split(";") if tok.strip()],
                "usable_reviews": org.usable_count,
                "reviews_in_corpus": len(corpus_reviews),
                "rating_distribution": rating_distribution(org.reviews),
            }
        )
    return organizations


def build_manifest(
    *,
    source_path: Path,
    source_format: str,
    stats: RecordStats,
    organizations: list[Organization],
    fleets: dict[str, Fleet],
    ids: dict[int, dict[str, str]],
    seed: int,
    target_per_category: int,
    min_per_org: int,
    out_dir: Path,
) -> dict[str, Any]:
    cand_by_category = {category: 0 for category in TARGET_CATEGORIES}
    for org in organizations:
        if org.category in cand_by_category and org.usable_count >= min_per_org:
            cand_by_category[org.category] += 1

    all_sampled = [review for fleet in fleets.values() for review in fleet.sampled]
    categories_blob: dict[str, Any] = {}
    for category in TARGET_CATEGORIES:
        fleet = fleets[category]
        sampled = fleet.sampled
        categories_blob[category] = {
            "target": target_per_category,
            "min_per_org": min_per_org,
            "sample_size": len(sampled),
            "source_rubrics": sorted({r.primary_rubric for r in sampled}),
            "cohort": fleet.cohort_stats(),
            "per_org_counts": fleet.per_org_counts,
            "rating_distribution": rating_distribution(sampled),
            "organizations": _org_dump(fleet),
        }

    reference_categories: dict[str, Any] = {}
    for category in _REFERENCE_CATEGORIES:
        ref_orgs = [org for org in organizations if org.category == category]
        try:
            ref_fleet = build_fleet(category, organizations, target_per_category, min_per_org)
        except NotEnoughReviewsError:
            ref_fleet = None
        if ref_fleet is not None:
            cohort = ref_fleet.cohort_stats()
        else:
            cohort = {
                "organizations": 0,
                "min_per_org": 0,
                "median_per_org": 0,
                "max_per_org": 0,
                "pool_size": 0,
                "sample_size": 0,
            }
        reference_categories[category] = {
            "source_rubric": _REFERENCE_SOURCE_RUBRICS.get(category, category),
            "included_in_corpus": False,
            "candidate_organizations_ge_min_per_org": sum(
                1 for org in ref_orgs if org.usable_count >= min_per_org
            ),
            "largest_organization_usable_reviews": max(
                (org.usable_count for org in ref_orgs), default=0
            ),
            "cohort_if_selected": cohort,
        }

    return {
        "corpus_name": CORPUS_NAME,
        "source_dataset": {
            "name": "Yandex Geo Reviews Dataset 2023",
            "official_repo": "https://github.com/yandex/geo-reviews-dataset-2023",
            "license": "MIT",
            "source_path_used": str(source_path),
            "source_format": source_format,
            "source_columns": ["address", "name_ru", "rubrics", "rating", "text"],
            "note": "Official TSKV release; the accepted parquet/csv mirrors carry "
            "the same 500,000 rows and columns.",
            "rows_loaded": stats.total,
            "rows_usable": stats.usable,
            "rows_skipped": {
                "rating_out_of_1_5": stats.skipped_rating,
                "no_text": stats.skipped_no_text,
                "no_name_or_address": stats.skipped_no_name,
                "total": stats.skipped_total(),
            },
        },
        "categories": categories_blob,
        "reference_categories": reference_categories,
        "candidate_organizations_ge_min_per_org": cand_by_category,
        "schema_conversion": {
            "target_schema": "ReviewScope NormalizedReview CSV ingestion schema",
            "column_mapping": {
                "place_name": "name_ru",
                "place_category": "primary rubric of the organization",
                "rating": "rating",
                "text": "text",
                "source": 'constant "yandex_geo_2023"',
                "review_id/place_id/reviewer_id": "stable local ids (see id_generation)",
            },
            "unavailable_fields": [
                "reviewer_name",
                "published_at",
                "city",
                "region",
                "country",
                "latitude",
                "longitude",
                "source_url",
            ],
        },
        "id_generation": {
            "place_id": "yg23-<sha256(name + address)[:16]> — one id per (name, address); "
            "distinct physical organizations are never merged",
            "review_id": "yg23-<sha256(place_id | rating | ordinal | text)[:24]> — stable across runs",
            "reviewer_id": "anon-<sha256(review_id)[:12]> — synthetic per-review opaque token, "
            "not a real user identity",
        },
        "seed": seed,
        "sampling": "target reviews are allocated across the fleet's organizations in "
        f"proportion to each organization's usable pool (largest-remainder integer "
        f"allocation) and then uniformly sampled per organization with a seeded RNG, so "
        f"per-place cohort sizes stay approximately balanced; the fleet pools the "
        f"largest-usable-count organizations (floor of {min_per_org} usable reviews per "
        f"organization) until it holds {target_per_category} reviews. Ratings are not "
        "stratified or rebalanced — the natural distribution is preserved.",
        "counts": {
            "per_category": {category: len(fleet.sampled) for category, fleet in fleets.items()},
            "total": len(all_sampled),
        },
        "rating_distributions": {
            category: rating_distribution(fleet.sampled) for category, fleet in fleets.items()
        }
        | {"overall": rating_distribution(all_sampled)},
        "suitable_for": [
            "templated detection",
            "specificity",
            "duplicate detection",
        ],
        "not_suitable_for": [
            "reviewer history validation",
            "local familiarity validation",
            "temporal burst validation",
            "travel-history validation",
        ],
        "analytical_limitations": [
            "The source dataset has no reviewer identity or reviewer history; reviewer_id is a "
            "synthetic per-review token so every review belongs to a distinct reviewer — "
            "reviewer-history, local-familiarity and travel-history signals are degenerate.",
            "The source dataset has no per-review timestamps — temporal burst validation cannot run.",
            "Pharmacy (rubric 'Аптека') was evaluated as the third category but no single pharmacy "
            "organization reaches the target size (top pharmacy org has 21 usable reviews; the "
            "top-K cohort would pool ~31 organizations with min/median/max 3/3/21). Pharmacy was "
            "therefore kept out of the 360-review corpus and appears only as reference "
            "statistics in this manifest.",
            "The shopping_center cohort uses the two largest organizations (Саларис, Авиапарк); "
            "per-place cohorts stay balanced by proportional per-organization sampling.",
            "The category pool exists only to reach the balanced evaluation size; every "
            "cohort-dependent ReviewScope signal still runs per place_id.",
            "An organization's row rubrics can vary across reviews in the source dataset; only "
            "reviews whose own rubric matches a target category join that category's pool, so "
            "an organization may contribute fewer reviews than its full review count.",
        ],
        "outputs": {
            "csv": str(out_dir / "real_reviews.csv"),
            "manifest": str(out_dir / "corpus_manifest.json"),
        },
        "generated_at_utc": _dt.datetime.now(_dt.UTC).isoformat(),
    }


def print_candidates(
    organizations: list[Organization],
    cats: tuple[str, ...],
    min_per_org: int,
    limit: int,
) -> None:
    for category in cats:
        candidates = [
            org
            for org in organizations
            if org.category == category and org.usable_count >= min_per_org
        ]
        candidates.sort(key=_org_sort_key)
        sizeable = sum(1 for org in candidates if org.usable_count >= DEFAULT_TARGET_PER_CATEGORY)
        print(
            f"\n=== CATEGORY: {category} — candidates >= {min_per_org} usable: "
            f"{len(candidates)} (with >= {DEFAULT_TARGET_PER_CATEGORY}: {sizeable}) ==="
        )
        for pos, org in enumerate(candidates[:limit], 1):
            name = org.name[:40]
            address = org.address[:52]
            dist = rating_distribution(org.reviews)
            dist_str = " ".join(f"{star}:{dist[star]}" for star in "12345")
            print(
                f"  {pos:>2}. {name:<40} | {address:<52} | {org.primary_rubric:<28} | "
                f"usable {org.usable_count:>4} | {dist_str}"
            )
        if len(candidates) > limit:
            print(
                f"  ... and {len(candidates) - limit} more "
                f"({len(candidates) - sizeable} below the single-organization target)"
            )


def print_fleet_summary(fleets: dict[str, Fleet]) -> None:
    print("\nSelected fleets (top-K until pooled >= target):")
    for category in TARGET_CATEGORIES:
        fleet = fleets[category]
        names = ", ".join(f"{org.name} ({len(org.reviews)} usable)" for org in fleet.organizations)
        print(f"  {category}: {len(fleet.organizations)} organization(s) — {names}")
        print(f"    pool {len(fleet.pool)} usable, sampled {len(fleet.sampled)}")
        stats = fleet.cohort_stats()
        print(
            f"    cohort per-org usable review counts: min {stats['min_per_org']}, "
            f"median {stats['median_per_org']}, max {stats['max_per_org']}"
        )


def verify_corpus(csv_path: Path, expected: int) -> None:
    """Load the produced corpus through the production CSVAdapter and verify it."""
    print(f"\nVerifying corpus through the production CSVAdapter: {csv_path}")
    result = CSVAdapter().load(csv_path)
    print(f"  rows imported: {result.report.total_rows}")
    print(f"  rows valid: {result.report.valid}")
    print(f"  rows skipped: {result.report.skipped}")
    print(f"  warnings: {result.report.warning_count}")
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
    print("  nullable-field report (None values across the corpus):")
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
    print(f"  ASSERT ok: exactly {expected} reviews loaded")
    if result.report.skipped:
        raise AssertionError(f"expected zero skipped rows, got {result.report.skipped}")
    for warning in result.report.warnings:
        print(f"    [warning] {warning}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a private ReviewScope validation corpus from the "
        "official Yandex Geo Reviews Dataset 2023."
    )
    parser.add_argument(
        "dataset", help="Path to the download (official .tskv, or .tskv/.csv/.parquet mirror)."
    )
    parser.add_argument(
        "--out-dir",
        default=("validation_data/private/yandex_geo_2023"),
        help="Output directory (default: validation_data/private/yandex_geo_2023).",
    )
    parser.add_argument(
        "--categories",
        default=",".join(TARGET_CATEGORIES),
        help="Comma-separated target categories.",
    )
    parser.add_argument("--target-per-category", type=int, default=DEFAULT_TARGET_PER_CATEGORY)
    parser.add_argument(
        "--min-per-org",
        type=int,
        default=DEFAULT_MIN_PER_ORG,
        help="Floor of usable reviews an organization must have to join a fleet.",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--format", choices=("auto", "tskv", "csv", "parquet"), default="auto")
    parser.add_argument(
        "--unescape-text",
        choices=("auto", "on", "off"),
        default="auto",
        help="Decode TSKV escapes in review text (auto: on for tskv/parquet).",
    )
    parser.add_argument("--candidates-limit", type=int, default=40)
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip loading the corpus back through the production CSVAdapter.",
    )
    args = parser.parse_args(argv)

    source_path = Path(args.dataset)
    if not source_path.is_file():
        parser.error(f"dataset file not found: {source_path}")

    cats = tuple(cat.strip() for cat in args.categories.split(",") if cat.strip())
    if cats != TARGET_CATEGORIES:
        unknown = set(cats) - set(TARGET_CATEGORIES)
        parser.error(f"unsupported categories {sorted(unknown)}; choices: {TARGET_CATEGORIES}")

    source_format = _detect_format(source_path, args.format)
    unescape = {"auto": source_format in ("tskv", "parquet"), "on": True, "off": False}[
        args.unescape_text
    ]

    print(f"Loading {source_path} (format: {source_format}) ...")
    records = load_tabular(source_path, source_format)
    reviews, stats = build_records(records, unescape_text=unescape)
    organizations = group_organizations(reviews)
    print(f"  rows loaded: {stats.total:,}")
    print(f"  usable reviews (non-empty text, rating 1..5): {stats.usable:,}")
    print(
        f"  skipped: rating_out_of_1_5={stats.skipped_rating}, "
        f"no_text={stats.skipped_no_text}, no_name_or_address={stats.skipped_no_name}"
    )
    print(f"  organizations: {len(organizations):,}")

    per_category = {
        category: sum(1 for org in organizations if org.category == category) for category in cats
    }
    print(f"  organizations by category: {per_category}")

    print_candidates(organizations, cats, args.min_per_org, args.candidates_limit)

    fleets: dict[str, Fleet] = {}
    for category in cats:
        fleet = build_fleet(category, organizations, args.target_per_category, args.min_per_org)
        sample_fleet(fleet, args.seed)
        fleets[category] = fleet
    print_fleet_summary(fleets)

    sampled = [review for fleet in fleets.values() for review in fleet.sampled]
    ids = make_ids(sampled)
    rows = build_csv_rows(fleets, ids)
    if len(rows) != len(cats) * args.target_per_category:
        raise AssertionError(
            f"expected {len(cats) * args.target_per_category} rows, got {len(rows)}"
        )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "real_reviews.csv"
    manifest_path = out_dir / "corpus_manifest.json"

    frame = pd.DataFrame.from_records(rows, columns=CSV_COLUMNS)
    frame.to_csv(csv_path, index=False, encoding="utf-8")
    manifest = build_manifest(
        source_path=source_path,
        source_format=source_format,
        stats=stats,
        organizations=organizations,
        fleets=fleets,
        ids=ids,
        seed=args.seed,
        target_per_category=args.target_per_category,
        min_per_org=args.min_per_org,
        out_dir=out_dir,
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\nWrote:")
    print(f"  {csv_path}")
    print(f"  {manifest_path}")
    for category in cats:
        dist = rating_distribution(fleets[category].sampled)
        summary = " ".join(f"{star}:{count}" for star, count in dist.items())
        print(f"  {category}: {len(fleets[category].sampled)} reviews {summary}")

    if not args.no_verify:
        verify_corpus(csv_path, expected=len(cats) * args.target_per_category)

    for category, fleet in fleets.items():
        cohort = fleet.cohort_stats()
        if cohort["organizations"] > 5:
            print(
                f"\nLIMITATION: the {category} cohort pools many small organizations "
                f"(min/median/max per org {cohort['min_per_org']}/"
                f"{cohort['median_per_org']}/{cohort['max_per_org']}); within-place peer "
                "context is thin, so place-level templated/duplicate signals are weakened."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
