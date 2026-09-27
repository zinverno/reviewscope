"""Prepare a private ReviewScope validation corpus from Google Local 2021 (UCSD), Vermont.

Selection is the Phase 17 feasibility configuration, reproduced verbatim:
``band80-150/cap250 N10/M3/P2``. Places are ranked by text-bearing review count,
the top ``PLACE_CAP`` places whose text-degree falls inside ``PLACE_BAND`` are
kept, and that subgraph is pruned by the bipartite filter until every place has
>= ``MIN_PLACE_REVIEWS`` reviews, every reviewer has >= ``MIN_REVIEWER_REVIEWS``
reviews and every reviewer has >= ``MIN_REVIEWER_PLACES`` distinct places.

No review-level sampling happens anywhere: whole place cohorts are kept, ratings
and categories are never rebalanced, and nothing is tuned against ReviewScope
scores. The run is a pure function of the official release, so it reruns
byte-identically.

Google Local ``time`` is unix *milliseconds*. Those integers are converted to
explicit UTC ISO-8601 here; ReviewScope's generic ``parse_date`` reads a bare
integer as unix *seconds*, which would silently land every review in 1970.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# --- Phase 17 feasibility configuration (band80-150/cap250 N10/M3/P2) -------

PLACE_BAND = (80, 150)
PLACE_CAP = 250
MIN_PLACE_REVIEWS = 10
MIN_REVIEWER_REVIEWS = 3
MIN_REVIEWER_PLACES = 2
MAX_FILTER_ROUNDS = 50

SOURCE = "google-local-2021-ucsd"
CONFIG_LABEL = "band80-150/cap250 N10/M3/P2"
REVIEW_SOURCE_URL = "https://mcauleylab.ucsd.edu/public_datasets/gdrive/googlelocal/"

# Fixed corpus salt: reviewer pseudonyms are stable across reruns of this corpus
# and do not depend on any secret. They are one-way identifiers, not anonymity
# against someone who already holds the public release.
PSEUDONYM_SALT = "reviewscope-phase17-google-local-2021-ucsd-vermont-v1"

CSV_FIELDS = [
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

VT_ZIP_RE = re.compile(r"(?:^|,\s*)([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$")
STREET_RE = re.compile(
    r"\b(st|street|ave|avenue|rd|road|hwy|highway|ln|lane|dr|drive|blvd|boulevard|ct|court"
    r"|cir|circle|way|pl|place|pkwy|parkway|ter|terrace|sq|square|plz|plaza|trl|trail"
    r"|rt|route|box|unit|ste|#)\b",
    re.IGNORECASE,
)


class ConflictingMetadataError(RuntimeError):
    """Raised when one gmap_id carries more than one non-identical metadata record."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_jsonl_gz(path: Path) -> Iterator[dict[str, Any]]:
    """Stream one JSON object per line out of a gzipped JSONL file."""
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def clean_text(value: Any) -> str | None:
    """Return the review text, or None when it is missing/blank.

    Whitespace-only text is treated as absent: ReviewScope's ``_coerce_str``
    strips and drops it anyway, so counting it as text-bearing would inflate the
    corpus with rows the analyzer rejects as ``missing review text``.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def count_text_degree(path: Path) -> tuple[Counter[str], dict[str, int]]:
    """Pass 1: text-bearing review counts per place, and the rating-only count."""
    degree: Counter[str] = Counter()
    total = 0
    rating_only = 0
    for record in iter_jsonl_gz(path):
        total += 1
        if clean_text(record.get("text")) is None:
            rating_only += 1
            continue
        degree[str(record.get("gmap_id"))] += 1
    return degree, {"rows": total, "rating_only": rating_only}


def select_places(text_degree: Counter[str]) -> dict[str, Any]:
    """Pick the place cohort: the busiest places inside the text-degree band."""
    ranked = sorted(text_degree.items(), key=lambda kv: (-kv[1], kv[0]))
    lo, hi = PLACE_BAND
    band = [gid for gid, deg in ranked if lo <= deg <= hi]
    selected = sorted(band[:PLACE_CAP])
    return {
        "places_in_band": len(band),
        "places_selected": selected,
        "band_low": lo,
        "band_high": hi,
        "cap": PLACE_CAP,
    }


def iterative_bipartite_filter(
    edges: list[tuple[str, str]],
    place_min: int = MIN_PLACE_REVIEWS,
    reviewer_min: int = MIN_REVIEWER_REVIEWS,
    reviewer_places_min: int = MIN_REVIEWER_PLACES,
    max_rounds: int = MAX_FILTER_ROUNDS,
) -> tuple[list[tuple[str, str]], int]:
    """Drop reviews until every place and reviewer clears the degree floors.

    ``edges`` are ``(place_id, reviewer_id)`` pairs. Returns the surviving edges
    and the number of rounds it took to reach a fixed point.
    """
    current = list(edges)
    for rounds in range(1, max_rounds + 1):
        place_degree: Counter[str] = Counter()
        reviewer_degree: Counter[str] = Counter()
        reviewer_places: dict[str, set[str]] = defaultdict(set)
        for place_id, reviewer_id in current:
            place_degree[place_id] += 1
            reviewer_degree[reviewer_id] += 1
            reviewer_places[reviewer_id].add(place_id)
        good_reviewers = {
            reviewer_id
            for reviewer_id, degree in reviewer_degree.items()
            if degree >= reviewer_min and len(reviewer_places[reviewer_id]) >= reviewer_places_min
        }
        kept = [
            (place_id, reviewer_id)
            for place_id, reviewer_id in current
            if place_degree[place_id] >= place_min and reviewer_id in good_reviewers
        ]
        if len(kept) == len(current):
            return kept, rounds
        current = kept
    return current, max_rounds


def canonical_record_fingerprint(record: dict[str, Any]) -> str:
    """Stable fingerprint of the *complete* source review record.

    Every published field takes part, so two rows collapse only when the source
    itself repeats them verbatim. Near-duplicates that share place, reviewer and
    timestamp but differ anywhere (including the reviewer name) stay separate.
    The human name never leaves the hash.
    """
    payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_candidate_edges(path: Path, allowed_places: set[str]) -> list[dict[str, Any]]:
    """Pass 2: load text-bearing reviews for the allowed places only.

    ``fingerprint`` identifies the complete source record so exact duplicates can
    be removed later; ``ordinal`` is the 0-based file line, kept only to make the
    canonical choice auditable.
    """
    rows: list[dict[str, Any]] = []
    for ordinal, record in enumerate(iter_jsonl_gz(path)):
        place_id = str(record.get("gmap_id"))
        if place_id not in allowed_places:
            continue
        text = clean_text(record.get("text"))
        if text is None:
            continue
        rating = record.get("rating")
        rows.append(
            {
                "place_id": place_id,
                "reviewer_id": str(record.get("user_id")),
                "time_ms": int(record.get("time")),
                "rating": int(rating) if isinstance(rating, (int, float)) else None,
                "text": text,
                "fingerprint": canonical_record_fingerprint(record),
                "ordinal": ordinal,
            }
        )
    return rows


def load_meta(path: Path) -> list[dict[str, Any]]:
    return [dict(record) for record in iter_jsonl_gz(path)]


def dedupe_meta(records: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Keep one record per gmap_id, refusing to guess between conflicting ones."""
    by_id: dict[str, dict[str, Any]] = {}
    seen_ids: Counter[str] = Counter()
    for record in records:
        gmap_id = str(record.get("gmap_id"))
        seen_ids[gmap_id] += 1
        existing = by_id.get(gmap_id)
        if existing is None:
            by_id[gmap_id] = record
            continue
        if existing != record:
            differing = sorted(
                key
                for key in set(existing) | set(record)
                if existing.get(key) != record.get(key)
            )
            raise ConflictingMetadataError(
                f"conflicting metadata for gmap_id {gmap_id!r} on fields {differing}; "
                "refusing to choose silently"
            )
    duplicated = sorted(gid for gid, count in seen_ids.items() if count > 1)
    stats = {
        "rows": len(records),
        "unique_gmap_ids": len(by_id),
        "duplicated_gmap_ids": len(duplicated),
        "duplicate_rows_removed": len(records) - len(by_id),
        "all_duplicates_identical": True,
        "duplicated_gmap_id_sample": duplicated[:10],
    }
    return by_id, stats


LATITUDE_KEYS = ("latitude", "lat")
LONGITUDE_KEYS = ("longitude", "long")


def resolve_coordinate_keys(sample: dict[str, Any]) -> tuple[str, str]:
    """Find the coordinate field names the release actually uses.

    The official dump spells them ``latitude``/``longitude``. Resolving instead of
    hardcoding keeps a schema change from silently writing a corpus with no
    coordinates at all.
    """
    latitude = next((key for key in LATITUDE_KEYS if key in sample), None)
    longitude = next((key for key in LONGITUDE_KEYS if key in sample), None)
    if latitude is None or longitude is None:
        raise KeyError(
            f"no coordinate fields in the metadata record; looked for "
            f"{LATITUDE_KEYS + LONGITUDE_KEYS}, found {sorted(sample)}"
        )
    return latitude, longitude


def primary_category(categories: Any) -> str | None:
    """First category in the published list, which is the most specific one.

    Google Local lists categories most-specific-first, so the head of the list is
    deterministic. Absent or empty lists stay empty rather than being guessed.
    """
    if categories is None:
        return None
    if isinstance(categories, str):
        candidates = [categories]
    else:
        try:
            candidates = list(categories)
        except TypeError:
            return None
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def parse_address(address: Any) -> tuple[str | None, str | None]:
    """Extract (city, region) conservatively from a US ``street, city, ST ZIP``.

    Both fields stay ``None`` unless the trailing two-letter state + ZIP pattern
    matches, and the city candidate must not look like a street line. A
    nullable field is better than a confidently wrong one.
    """
    if not isinstance(address, str) or not address.strip():
        return None, None
    text = unicodedata.normalize("NFKC", address).strip()
    match = VT_ZIP_RE.search(text)
    if not match:
        return None, None
    region = match.group(1)
    head = text[: match.start()].rstrip().rstrip(",").strip()
    if not head:
        return None, region
    city = head.rsplit(",", 1)[-1].strip()
    if not city or city[0].isdigit() or STREET_RE.search(city):
        return None, region
    return city, region


def ms_to_published_at(time_ms: int) -> str:
    """Convert Google Local unix *milliseconds* to ReviewScope-safe ISO-8601 UTC.

    The microsecond field is always written out. ``isoformat()`` omits it when
    the value is exactly on a second, and ReviewScope's
    ``pd.to_datetime(..., errors="coerce")`` infers a single format from the
    first value in the column -- so a column that mixes ``.123000`` with a
    bare ``:00`` silently coerces the bare rows to ``NaT`` and drops them from
    temporal analysis. One fixed precision keeps every row parseable.
    """
    moment = datetime.fromtimestamp(time_ms / 1000.0, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def reviewer_pseudonym(user_id: str, salt: str = PSEUDONYM_SALT) -> str:
    """Stable opaque reviewer id. The source user_id never leaves this function."""
    digest = hashlib.sha256(f"{salt}\x1f{user_id}".encode()).hexdigest()
    return f"vt21u-{digest[:16]}"


def derive_review_id(
    place_id: str,
    source_user_id: str,
    time_ms: int,
    rating: Any,
    text: str,
    fingerprint: str = "",
) -> str:
    """Stable review id from source identity, content and full-record fingerprint.

    Exact source duplicates are removed before ids are generated, so no file
    position is needed. The full-record fingerprint is folded in so two records
    that share place, reviewer, time, rating and text but differ in any other
    published field (the reviewer name, for example) still get distinct ids,
    which matters because review_id is the storage primary key.
    """
    payload = f"{place_id}\x1f{source_user_id}\x1f{time_ms}\x1f{rating}\x1f{text}\x1f{fingerprint}"
    return "vt21r-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def dedupe_exact_duplicates(
    selected: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop byte-identical source records, keeping the first occurrence.

    The retained occurrence is the earliest one in file order, so the choice is
    deterministic and does not depend on dict or set iteration.
    """
    kept: list[dict[str, Any]] = []
    first_seen: dict[str, int] = {}
    removed = 0
    for candidate in selected:
        fingerprint = candidate["fingerprint"]
        if fingerprint in first_seen:
            removed += 1
            continue
        first_seen[fingerprint] = candidate["ordinal"]
        kept.append(candidate)
    return kept, {
        "selected_source_rows_before_dedupe": len(selected),
        "exact_duplicate_records_removed": removed,
        "unique_canonical_reviews_after_dedupe": len(kept),
        "fingerprint_method": (
            "sha256 over json.dumps of the complete source record, keys sorted and "
            "separators compact, i.e. every published field including name, pics and "
            "resp must match"
        ),
        "canonical_occurrence": "earliest line in the official file wins; order-independent",
        "rationale": (
            "the official release repeats some review records verbatim. They are a "
            "source duplication artifact, not distinct reviews, and keeping them would "
            "inflate duplicate detection, reviewer statistics and coordinated-activity "
            "analysis. Deduped before review_id generation, so review_id needs no file "
            "position to disambiguate"
        ),
    }


def build_rows(
    selected: set[tuple[str, str]],
    candidates: list[dict[str, Any]],
    meta_by_id: dict[str, dict[str, Any]],
    salt: str = PSEUDONYM_SALT,
    latitude_key: str = "latitude",
    longitude_key: str = "longitude",
) -> list[dict[str, Any]]:
    """Map selected reviews onto the ReviewScope CSV schema."""
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        key = (candidate["place_id"], candidate["reviewer_id"])
        if key not in selected:
            continue
        place_id = candidate["place_id"]
        meta = meta_by_id.get(place_id, {})
        city, region = parse_address(meta.get("address"))
        rows.append(
            {
                "review_id": derive_review_id(
                    place_id,
                    candidate["reviewer_id"],
                    candidate["time_ms"],
                    candidate["rating"],
                    candidate["text"],
                    candidate["fingerprint"],
                ),
                "place_id": place_id,
                "place_name": meta.get("name") or None,
                "place_category": primary_category(meta.get("category")),
                "reviewer_id": reviewer_pseudonym(candidate["reviewer_id"], salt),
                "reviewer_name": None,
                "rating": candidate["rating"],
                "text": candidate["text"],
                "published_at": ms_to_published_at(candidate["time_ms"]),
                "city": city,
                "region": region,
                "country": "US",
                "latitude": meta.get(latitude_key),
                "longitude": meta.get(longitude_key),
                "source": SOURCE,
                "source_url": None,
            }
        )
    rows.sort(key=lambda row: row["review_id"])
    return rows


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile, matching pandas' default method."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return float(ordered[int(pos)])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def describe(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 3) if values else 0.0,
        "p25": round(percentile(values, 0.25), 3),
        "median": round(percentile(values, 0.5), 3),
        "p75": round(percentile(values, 0.75), 3),
        "p90": round(percentile(values, 0.9), 3),
        "max": round(max(values), 3) if values else 0.0,
    }


def connected_components(edges: Iterable[tuple[str, str]]) -> list[int]:
    """Union-find over reviewer/place nodes; returns component size per node."""
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for place_id, reviewer_id in edges:
        a, b = find(f"p:{place_id}"), find(f"u:{reviewer_id}")
        if a != b:
            parent[max(a, b)] = min(a, b)
    sizes: Counter[str] = Counter()
    for node in list(parent):
        sizes[find(node)] += 1
    return list(sizes.values())


def compute_stats(
    rows: list[dict[str, Any]],
    source_counts: dict[str, int],
    meta_stats: dict[str, Any],
    dedupe_stats: dict[str, Any],
) -> dict[str, Any]:
    """Every number the brief asks for, computed from the written rows."""
    place_counts: Counter[str] = Counter()
    reviewer_counts: Counter[str] = Counter()
    reviewer_places: dict[str, set[str]] = defaultdict(set)
    reviewer_categories: dict[str, set[str]] = defaultdict(set)
    ratings: Counter[int] = Counter()
    category_reviews: Counter[str] = Counter()
    category_places: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        place_counts[row["place_id"]] += 1
        reviewer_counts[row["reviewer_id"]] += 1
        reviewer_places[row["reviewer_id"]].add(row["place_id"])
        reviewer_categories[row["reviewer_id"]].add(row["place_category"] or "(uncategorised)")
        if row["rating"] is not None:
            ratings[row["rating"]] += 1
        category = row["place_category"] or "(uncategorised)"
        category_reviews[category] += 1
        category_places[category].add(row["place_id"])

    components = connected_components(
        [(row["place_id"], row["reviewer_id"]) for row in rows]
    )
    components.sort(reverse=True)

    published = sorted(row["published_at"] for row in rows)
    with_coords = sum(
        1 for row in rows if row["latitude"] is not None and row["longitude"] is not None
    )
    unique_places = {row["place_id"] for row in rows}
    city_rows = sum(1 for row in rows if row["city"])
    region_rows = sum(1 for row in rows if row["region"])

    return {
        "reviews": {
            "total": len(rows),
            "text_bearing": len(rows),
            "rating_distribution": {str(k): ratings[k] for k in sorted(ratings)},
            "timestamp_min": published[0] if published else None,
            "timestamp_max": published[-1] if published else None,
            "distinct_years": len({row["published_at"][:4] for row in rows}),
        },
        "dense_graph_check": {
            "places_below_min_reviews": sorted(
                place for place, count in place_counts.items() if count < MIN_PLACE_REVIEWS
            ),
            "reviewers_below_min_reviews": sorted(
                reviewer
                for reviewer, count in reviewer_counts.items()
                if count < MIN_REVIEWER_REVIEWS
            ),
            "reviewers_below_min_places": sorted(
                reviewer
                for reviewer, places in reviewer_places.items()
                if len(places) < MIN_REVIEWER_PLACES
            ),
            "thresholds": {
                "min_reviews_per_place": MIN_PLACE_REVIEWS,
                "min_reviews_per_reviewer": MIN_REVIEWER_REVIEWS,
                "min_places_per_reviewer": MIN_REVIEWER_PLACES,
            },
            "clean": None,  # filled in by the caller, which knows the dedupe totals
        },
        "excluded": {
            "rating_only_reviews_in_source": source_counts["rating_only"],
            "source_review_rows": source_counts["rows"],
            "text_bearing_reviews_in_source": source_counts["rows"] - source_counts["rating_only"],
            "note": "rating-only reviews are excluded from the primary corpus by design",
        },
        "source_duplicates": dedupe_stats,
        "places": {
            "total": len(unique_places),
            "reviews_per_place": describe(list(place_counts.values())),
        },
        "reviewers": {
            "total": len(reviewer_counts),
            "reviews_per_reviewer": describe(list(reviewer_counts.values())),
            "distinct_places_per_reviewer": describe(
                [len(places) for places in reviewer_places.values()]
            ),
            "reviewers_with_2plus_places": sum(1 for v in reviewer_places.values() if len(v) >= 2),
            "reviewers_with_3plus_places": sum(1 for v in reviewer_places.values() if len(v) >= 3),
            "reviewers_with_5plus_places": sum(1 for v in reviewer_places.values() if len(v) >= 5),
            "reviewers_with_10plus_places": sum(
                1 for v in reviewer_places.values() if len(v) >= 10
            ),
            "categories_per_reviewer": describe(
                [len(cats) for cats in reviewer_categories.values()]
            ),
        },
        "categories": {
            "total": len(category_reviews),
            "top_by_reviews": category_reviews.most_common(15),
            "top_by_places": sorted(
                ((cat, len(places)) for cat, places in category_places.items()),
                key=lambda kv: (-kv[1], kv[0]),
            )[:15],
        },
        "graph": {
            "connected_components": len(components),
            "component_sizes_top5": components[:5],
            "largest_component_share": round(
                (components[0] / (len(unique_places) + len(reviewer_counts))), 6
            )
            if components
            else 0.0,
            "nodes": len(unique_places) + len(reviewer_counts),
        },
        "coordinates": {
            "review_rows_with_coordinates": with_coords,
            "review_coverage_pct": round(100.0 * with_coords / len(rows), 4) if rows else 0.0,
            "places_with_coordinates": len(
                {
                    row["place_id"]
                    for row in rows
                    if row["latitude"] is not None and row["longitude"] is not None
                }
            ),
            "place_coverage_pct": round(
                100.0
                * len(
                    {
                        row["place_id"]
                        for row in rows
                        if row["latitude"] is not None and row["longitude"] is not None
                    }
                )
                / len(unique_places),
                4,
            )
            if unique_places
            else 0.0,
        },
        "address_parsing": {
            "rows_with_city": city_rows,
            "rows_with_region": region_rows,
            "policy": "nullable: left empty unless a trailing ST+ZIP match succeeds",
        },
        "metadata": meta_stats,
    }


def write_corpus(rows: list[dict[str, Any]], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "reviews.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: "" if row.get(key) is None else row[key] for key in CSV_FIELDS})
    return csv_path


def verify_with_adapter(csv_path: Path) -> dict[str, Any]:
    """Load the written corpus back through the real production CSVAdapter."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from reviewscope.ingestion.csv_adapter import CSVAdapter  # noqa: PLC0415

    result = CSVAdapter().load(csv_path)
    report = result.report
    return {
        "imported": report.total_rows,
        "valid": report.valid,
        "skipped": report.skipped,
        "warnings": report.warning_count,
        "warning_sample": report.warnings[:10],
    }


def run(
    review_path: Path,
    meta_path: Path,
    out_dir: Path,
    salt: str = PSEUDONYM_SALT,
) -> dict[str, Any]:
    """Build the corpus end to end and return the manifest."""
    print(f"[1/7] scanning text degree: {review_path}")
    text_degree, source_counts = count_text_degree(review_path)
    print(
        f"      rows={source_counts['rows']:,} text-bearing={source_counts['rows'] - source_counts['rating_only']:,} "
        f"rating-only={source_counts['rating_only']:,} places={len(text_degree):,}"
    )

    print("[2/7] selecting the place cohort")
    place_choice = select_places(text_degree)
    allowed = set(place_choice["places_selected"])
    print(
        f"      band {place_choice['band_low']}-{place_choice['band_high']} "
        f"matches {place_choice['places_in_band']:,} places, keeping {len(allowed)}"
    )

    print("[3/7] loading candidates and filtering the graph")
    candidates = load_candidate_edges(review_path, allowed)
    edges = [(row["place_id"], row["reviewer_id"]) for row in candidates]
    kept, rounds = iterative_bipartite_filter(edges)
    selected = set(kept)
    print(
        f"      candidates={len(edges):,} -> selected={len(kept):,} "
        f"(filter reached a fixed point in {rounds} rounds)"
    )

    print("[4/7] removing byte-identical duplicate source records")
    selected_rows = [c for c in candidates if (c["place_id"], c["reviewer_id"]) in selected]
    canonical, dedupe_stats = dedupe_exact_duplicates(selected_rows)
    canonical_ids = {(c["place_id"], c["reviewer_id"]) for c in canonical}
    print(
        f"      selected rows={dedupe_stats['selected_source_rows_before_dedupe']:,} "
        f"-> removed {dedupe_stats['exact_duplicate_records_removed']:,} exact duplicates "
        f"-> {dedupe_stats['unique_canonical_reviews_after_dedupe']:,} canonical reviews"
    )

    print("[5/7] loading and deduplicating metadata")
    meta_records = load_meta(meta_path)
    meta_by_id, meta_stats = dedupe_meta(meta_records)
    print(
        f"      rows={meta_stats['rows']:,} unique={meta_stats['unique_gmap_ids']:,} "
        f"duplicated gmap_ids={meta_stats['duplicated_gmap_ids']} "
        f"all identical={meta_stats['all_duplicates_identical']}"
    )
    latitude_key, longitude_key = resolve_coordinate_keys(meta_records[0])
    print(f"      coordinate fields: {latitude_key} / {longitude_key}")
    missing_meta = sorted({place for place, _ in kept} - set(meta_by_id))
    if missing_meta:
        raise RuntimeError(
            f"{len(missing_meta)} selected places have no metadata record, "
            f"first: {missing_meta[:5]}"
        )

    print("[6/7] mapping rows and writing the corpus")
    rows = build_rows(
        canonical_ids, canonical, meta_by_id, salt=salt,
        latitude_key=latitude_key, longitude_key=longitude_key,
    )
    csv_path = write_corpus(rows, out_dir)
    ids = [row["review_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise RuntimeError(
            f"review_id is not unique ({len(ids) - len(set(ids))} collisions) after "
            "dedupe; near-duplicates remain that share place, reviewer, time, rating and text"
        )
    stats = compute_stats(rows, dict(source_counts), meta_stats, dedupe_stats)
    check = stats["dense_graph_check"]
    violations = (
        len(check["places_below_min_reviews"])
        + len(check["reviewers_below_min_reviews"])
        + len(check["reviewers_below_min_places"])
    )
    check["clean"] = violations == 0
    check["violations"] = violations
    if rows and stats["coordinates"]["place_coverage_pct"] == 0.0:
        raise RuntimeError(
            f"no selected place has {latitude_key}/{longitude_key}; the metadata join "
            "produced a corpus with no coordinates"
        )
    print(
        f"      wrote {len(rows):,} rows -> {csv_path}\n"
        f"      places={stats['places']['total']} reviewers={stats['reviewers']['total']} "
        f"categories={stats['categories']['total']} "
        f"components={stats['graph']['connected_components']} "
        f"dense-graph violations={violations}"
    )

    manifest = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "corpus": CONFIG_LABEL,
        "config": {
            "place_text_band": list(PLACE_BAND),
            "place_cap": PLACE_CAP,
            "min_reviews_per_place": MIN_PLACE_REVIEWS,
            "min_reviews_per_reviewer": MIN_REVIEWER_REVIEWS,
            "min_places_per_reviewer": MIN_REVIEWER_PLACES,
            "filter_rounds": rounds,
            "places_in_band": place_choice["places_in_band"],
            "places_selected": len(allowed),
        },
        "source": {
            "dataset": "Google Local 2021 (UCSD), Vermont 10-core release",
            "url": REVIEW_SOURCE_URL,
            "reviews_path": str(review_path),
            "reviews_sha256": sha256_file(review_path),
            "meta_path": str(meta_path),
            "meta_sha256": sha256_file(meta_path),
            "review_rows": source_counts["rows"],
            "text_bearing_rows": source_counts["rows"] - source_counts["rating_only"],
            "rating_only_rows_excluded": source_counts["rating_only"],
        },
        "privacy": {
            "reviewer_id": "sha256(salt || source user_id)[:16], prefixed 'vt21u-'",
            "pseudonym_salt": salt,
            "source_user_id_in_outputs": False,
            "reviewer_name_retained": False,
            "reviewer_human_name_dropped_at_read": True,
            "home_or_work_inference": "not attempted",
            "note": (
                "pseudonyms are deterministic and one-way; they are not anonymity "
                "against a holder of the public release"
            ),
        },
        "timestamp": {
            "source_unit": "unix milliseconds",
            "conversion": "datetime.fromtimestamp(ms/1000, tz=UTC).strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')",
            "note": (
                "ReviewScope's parse_date reads a bare integer as unix seconds, so "
                "raw ms integers are never written to the CSV. Microseconds are "
                "always written, never omitted on the second, because "
                "pd.to_datetime infers one format per column and would coerce "
                "the bare rows to NaT"
            ),
        },
        "id_rules": {
            "place_id": "source gmap_id, unchanged",
            "reviewer_id": "opaque salted hash of source user_id",
            "review_id": (
                "sha256(place_id || user_id || time_ms || rating || text || "
                "full_record_fingerprint)[:24], prefixed 'vt21r-'; exact source "
                "duplicates are removed first, so no file position is needed, and the "
                "fingerprint keeps near-duplicates that differ in any other field apart"
            ),
        },
        "category_rule": (
            "first entry of the metadata 'category' list as published (Google Local "
            "orders most-specific first); empty when the list is absent"
        ),
        "coordinate_fields": {"latitude": latitude_key, "longitude": longitude_key},
        "metadata_dedup": meta_stats,
        "source_review_dedup": dedupe_stats,
        "csv_fields": CSV_FIELDS,
        "outputs": {"reviews_csv": str(csv_path)},
        "stats": stats,
    }

    print("[7/7] loading the corpus back through the production CSVAdapter")
    manifest["ingestion_verification"] = verify_with_adapter(csv_path)
    (out_dir / "corpus_stats.json").write_text(
        json.dumps(stats, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    (out_dir / "corpus_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Prepare a private ReviewScope validation corpus from the official "
        "Google Local 2021 (UCSD) Vermont 10-core release."
    )
    parser.add_argument(
        "--source-dir",
        default="~/datasets/google-local-2021/vermont",
        help="Directory holding review-Vermont_10.json.gz and meta-Vermont.json.gz.",
    )
    parser.add_argument(
        "--out-dir",
        default="validation_data/private/google_local_vermont/rich_corpus",
        help="Output directory (stays gitignored).",
    )
    parser.add_argument("--salt", default=PSEUDONYM_SALT, help="Reviewer pseudonym salt.")
    args = parser.parse_args(argv)

    source_dir = Path(args.source_dir).expanduser()
    review_path = source_dir / "review-Vermont_10.json.gz"
    meta_path = source_dir / "meta-Vermont.json.gz"
    for path in (review_path, meta_path):
        if not path.is_file():
            parser.error(f"source file not found: {path}")

    manifest = run(review_path, meta_path, Path(args.out_dir), salt=args.salt)
    ingestion = manifest["ingestion_verification"]
    print(
        f"\nimported={ingestion['imported']:,} valid={ingestion['valid']:,} "
        f"skipped={ingestion['skipped']} warnings={ingestion['warnings']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
