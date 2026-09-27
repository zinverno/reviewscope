"""Deterministic tests for the Google Local 2021 (UCSD) Vermont corpus converter.

The converter runs against the official UCSD release, so these tests feed it a
tiny synthetic JSONL dump (never the real corpus, which stays gitignored). The
fixture is built to exercise the failure modes that matter: millisecond
timestamps, duplicate and conflicting metadata, and the bipartite filter.
"""

from __future__ import annotations

import csv
import gzip
import importlib.util
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
_MODULE_NAME = "prepare_google_local_vermont"


def _load_script():
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SCRIPTS / f"{_MODULE_NAME}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


gl = _load_script()

# Fixture shape: 2 cohort places x 8 reviewers x 2 reviews each = 16 text reviews,
# 8 reviewers with 2 places and 4 reviews apiece, so the real floors (N=10, M=3,
# P=2) all pass. PLACE_TINY is below the place floor, PLACE_WIDE is outside the
# band, and four rating-only rows sit on PLACE_A.
COHORT_PLACES = ("PLACE_A", "PLACE_B")
COHORT_REVIEWERS = 8
REVIEWS_PER_REVIEWER_PER_PLACE = 2
RATING_ONLY_ROWS = 4


def ms(year: int, month: int, day: int, hour: int = 12) -> int:
    """A millisecond timestamp for a known UTC instant."""
    return int(datetime(year, month, day, hour, tzinfo=UTC).timestamp() * 1000)


def meta_record(
    gmap_id: str,
    *,
    name: str = "Test Pizza",
    address: str = "Main St Test, 1 Main St, Rutland, VT 05701",
    category: list[str] | None = None,
    latitude: float = 43.61,
    longitude: float = -72.97,
) -> dict:
    return {
        "name": name,
        "address": address,
        "gmap_id": gmap_id,
        "description": "",
        "latitude": latitude,
        "longitude": longitude,
        "category": ["Pizza restaurant", "Restaurant"] if category is None else category,
        "avg_rating": 4.0,
        "num_of_reviews": 100,
        "price": "$$",
        "hours": None,
        "MISC": None,
        "relative_results": None,
        "url": None,
        "state": "open",
    }


def review_record(
    gmap_id: str,
    user_id: str,
    *,
    time_ms: int = ms(2019, 5, 3),
    rating: int = 5,
    text: str | None = "Solid pie, great crust.",
    name: str = "Reviewer Human Name",
) -> dict:
    """A review carrying the reviewer's real name, which the converter must drop."""
    return {
        "user_id": user_id,
        "name": name,
        "time": time_ms,
        "rating": rating,
        "text": text,
        "pics": None,
        "resp": None,
        "gmap_id": gmap_id,
    }


def write_gz_jsonl(path: Path, records: list[dict]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def build_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Write a synthetic release: one compliant cohort plus decoys."""
    reviews: list[dict] = []
    for place_id in COHORT_PLACES:
        for reviewer in range(COHORT_REVIEWERS):
            for visit in range(REVIEWS_PER_REVIEWER_PER_PLACE):
                reviews.append(
                    review_record(
                        place_id,
                        f"user-{reviewer:02d}",
                        time_ms=ms(2019, 5, 3) + (reviewer * 2 + visit) * 86_400_000,
                        rating=(reviewer % 5) + 1,
                        text=f"Review {reviewer}-{visit} about {place_id}.",
                    )
                )
    # Rating-only rows on a cohort place: excluded by the text filter.
    for index in range(RATING_ONLY_ROWS):
        reviews.append(review_record("PLACE_A", f"rating-user-{index}", text=None))
    # Below the place floor and the reviewer floors: must be filtered away.
    reviews.append(review_record("PLACE_TINY", "tiny-user", text="Only review here."))
    # Busy but outside the band: must be excluded by the band, not the filter.
    for index in range(30):
        reviews.append(review_record("PLACE_WIDE", f"wide-user-{index:02d}", text=f"Wide {index}."))

    meta: list[dict] = [
        meta_record("PLACE_A"),
        meta_record("PLACE_B", name="Big Grocery", category=["Grocery store", "Supermarket"]),
        meta_record("PLACE_TINY", name="Tiny Cafe", category=["Cafe"]),
        meta_record("PLACE_WIDE", name="Wide Diner", category=["Diner"]),
    ]
    # Verbatim duplicate of a cohort place: the benign duplicate case.
    meta.append(dict(meta_record("PLACE_A")))

    review_path = write_gz_jsonl(tmp_path / "review-Vermont_10.json.gz", reviews)
    meta_path = write_gz_jsonl(tmp_path / "meta-Vermont.json.gz", meta)
    return review_path, meta_path


@pytest.fixture(autouse=True)
def small_band(monkeypatch):
    """Shrink the place band so the tiny fixture is selectable.

    The reviewer and place floors stay at their production values, so the filter
    itself is still exercised for real.
    """
    monkeypatch.setattr(gl, "PLACE_BAND", (10, 20))
    monkeypatch.setattr(gl, "PLACE_CAP", 10)


def run_fixture(tmp_path: Path, name: str = "out") -> Path:
    review_path, meta_path = build_fixture(tmp_path)
    out_dir = tmp_path / name
    gl.run(review_path, meta_path, out_dir)
    return out_dir


def load_corpus_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


# --- millisecond timestamp conversion ------------------------------------


def test_ms_converts_to_expected_utc_date():
    """A known millisecond timestamp must land in the expected 20xx UTC date."""
    converted = gl.ms_to_published_at(ms(2019, 5, 3))
    assert converted.startswith("2019-05-03T12:00:00")
    assert converted.endswith("+00:00")


def test_ms_conversion_is_exact_to_the_millisecond():
    assert gl.ms_to_published_at(1_556_972_800_123) == "2019-05-04T12:26:40.123000+00:00"


def test_raw_ms_through_production_parse_date_is_not_the_real_date():
    """Documents the unit trap the converter exists to avoid.

    ``parse_date`` reads a bare integer as unix *seconds*; a millisecond value is
    so far in the future that it cannot be represented at all, so the generic
    path silently loses the date instead of returning it.
    """
    from reviewscope.ingestion.normalize import parse_date

    raw = ms(2019, 5, 3)
    assert parse_date(raw) is None
    assert parse_date(str(raw)) is None
    assert parse_date(gl.ms_to_published_at(raw)).year == 2019


def test_no_converted_review_lands_in_1970(tmp_path):
    """No corpus row may carry a 1970 date, and the range must be plausible."""
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    assert rows
    for row in rows:
        assert row["published_at"].endswith("+00:00")
        assert not row["published_at"].startswith("1970")
        assert 2007 <= int(row["published_at"][:4]) <= 2021

    stats = json.loads((out_dir / "corpus_stats.json").read_text())
    assert stats["reviews"]["timestamp_min"].startswith("2019-")
    assert stats["reviews"]["timestamp_max"].startswith("2019-")


# --- reviewer pseudonym stability ----------------------------------------


def test_pseudonym_is_stable_and_opaque():
    first = gl.reviewer_pseudonym("user-01")
    assert first == gl.reviewer_pseudonym("user-01")
    assert first != gl.reviewer_pseudonym("user-02")
    assert first.startswith("vt21u-")
    assert "user-01" not in first


def test_pseudonym_depends_on_salt():
    assert gl.reviewer_pseudonym("user-01", salt="a") != gl.reviewer_pseudonym("user-01", salt="b")


def test_pseudonyms_survive_a_reload(tmp_path):
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")

    manifest = json.loads((out_dir / "corpus_manifest.json").read_text())
    assert manifest["privacy"]["source_user_id_in_outputs"] is False
    assert manifest["privacy"]["reviewer_name_retained"] is False
    for row in rows:
        assert row["reviewer_id"].startswith("vt21u-")
        assert row["reviewer_name"] == ""
        assert "user-" not in row["reviewer_id"]
        # Neither the source id nor the human name may leak into any field.
        assert "Reviewer Human Name" not in json.dumps(row)
        assert "rating-user" not in json.dumps(row)


def test_reviewer_across_places_shares_one_pseudonym(tmp_path):
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    counts = Counter(row["reviewer_id"] for row in rows)
    assert len(counts) == COHORT_REVIEWERS
    # Each reviewer visited both cohort places, so each pseudonym recurs.
    assert set(counts.values()) == {len(COHORT_PLACES) * REVIEWS_PER_REVIEWER_PER_PLACE}
    for reviewer_id in counts:
        places = {row["place_id"] for row in rows if row["reviewer_id"] == reviewer_id}
        assert places == set(COHORT_PLACES)


# --- metadata duplicate handling ----------------------------------------


def test_identical_duplicates_are_deduplicated():
    records = [meta_record("A"), meta_record("A"), meta_record("B")]
    by_id, stats = gl.dedupe_meta(records)
    assert set(by_id) == {"A", "B"}
    assert stats["rows"] == 3
    assert stats["unique_gmap_ids"] == 2
    assert stats["duplicated_gmap_ids"] == 1
    assert stats["duplicate_rows_removed"] == 1
    assert stats["all_duplicates_identical"] is True


def test_conflicting_metadata_raises_instead_of_choosing():
    records = [meta_record("A", name="Alpha"), meta_record("A", name="Beta")]
    with pytest.raises(gl.ConflictingMetadataError) as excinfo:
        gl.dedupe_meta(records)
    assert "conflicting metadata" in str(excinfo.value)
    assert "name" in str(excinfo.value)


def test_conflicting_metadata_stops_the_run(tmp_path):
    review_path, _ = build_fixture(tmp_path)
    write_gz_jsonl(
        tmp_path / "meta-Vermont.json.gz",
        [meta_record("PLACE_A", name="One"), meta_record("PLACE_A", name="Two")],
    )
    with pytest.raises(gl.ConflictingMetadataError):
        gl.run(review_path, tmp_path / "meta-Vermont.json.gz", tmp_path / "out")
    assert not (tmp_path / "out" / "reviews.csv").exists()


def test_duplicate_stats_are_recorded_in_the_manifest(tmp_path):
    out_dir = run_fixture(tmp_path)
    manifest = json.loads((out_dir / "corpus_manifest.json").read_text())
    assert manifest["metadata_dedup"]["duplicated_gmap_ids"] == 1
    assert manifest["metadata_dedup"]["all_duplicates_identical"] is True
    assert manifest["metadata_dedup"]["rows"] == 5
    assert manifest["metadata_dedup"]["unique_gmap_ids"] == 4


# --- category mapping -----------------------------------------------------


def test_primary_category_takes_the_published_head():
    assert gl.primary_category(["Pizza restaurant", "Restaurant"]) == "Pizza restaurant"
    assert gl.primary_category(["Grocery store", "Supermarket"]) == "Grocery store"


def test_primary_category_of_missing_or_blank_input_is_empty():
    assert gl.primary_category(None) is None
    assert gl.primary_category([]) is None
    assert gl.primary_category(["", "   "]) is None
    assert gl.primary_category("Solo category") == "Solo category"


def test_categories_survive_ingestion(tmp_path):
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    mapping = {row["place_id"]: row["place_category"] for row in rows}
    assert mapping == {"PLACE_A": "Pizza restaurant", "PLACE_B": "Grocery store"}


# --- address mapping ------------------------------------------------------


def test_address_parsing_extracts_city_and_region():
    assert gl.parse_address("Some Name, 150 Woodstock Ave, Rutland, VT 05701") == ("Rutland", "VT")


def test_address_parsing_refuses_to_guess():
    assert gl.parse_address("Some Name, 1 Main St, VT 05701") == (None, "VT")
    assert gl.parse_address("No zip here, Some Town") == (None, None)
    assert gl.parse_address(None) == (None, None)
    assert gl.parse_address("") == (None, None)


# --- text-bearing definition --------------------------------------------


def test_blank_and_whitespace_text_is_not_text_bearing():
    assert gl.clean_text(None) is None
    assert gl.clean_text("") is None
    assert gl.clean_text("   \n ") is None
    assert gl.clean_text("  real  ") == "real"


def test_rating_only_reviews_are_excluded_and_recorded(tmp_path):
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    assert all(row["text"].strip() for row in rows)
    assert len(rows) == len(COHORT_PLACES) * COHORT_REVIEWERS * REVIEWS_PER_REVIEWER_PER_PLACE

    stats = json.loads((out_dir / "corpus_stats.json").read_text())
    assert stats["excluded"]["rating_only_reviews_in_source"] == RATING_ONLY_ROWS
    assert stats["reviews"]["text_bearing"] == stats["reviews"]["total"]


# --- selection / graph invariants ----------------------------------------


def test_iterative_filter_drops_places_and_reviewers_below_floors():
    # PLACE_A: 3 reviewers x 2 reviews. PLACE_B: 1 reviewer x 10 reviews, whose
    # single place leaves it below the P=2 floor, so nothing survives.
    edges = [("A", f"u{i}") for i in range(3) for _ in range(2)]
    edges += [("B", "lonely")] * 10
    kept, _ = gl.iterative_bipartite_filter(edges, 5, 2, 2)
    assert kept == []


def test_iterative_filter_keeps_a_compliant_graph():
    edges = [("A", f"u{i}") for i in range(5)]
    edges += [("B", f"u{i}") for i in range(5)]
    kept, rounds = gl.iterative_bipartite_filter(edges, 5, 2, 2)
    assert len(kept) == 10
    assert rounds == 1


def test_iterative_filter_is_order_independent():
    edges = [("A", f"u{i}") for i in range(5)] + [("B", f"u{i}") for i in range(5)]
    forward, _ = gl.iterative_bipartite_filter(edges, 5, 2, 2)
    backward, _ = gl.iterative_bipartite_filter(list(reversed(edges)), 5, 2, 2)
    assert sorted(forward) == sorted(backward)


def test_place_selection_uses_the_band_and_cap(monkeypatch):
    monkeypatch.setattr(gl, "PLACE_BAND", (2, 4))
    monkeypatch.setattr(gl, "PLACE_CAP", 2)
    choice = gl.select_places(Counter({"A": 10, "B": 4, "C": 3, "D": 2, "E": 9}))
    # A and E are too big; the band holds B, C, D; the cap keeps the two busiest.
    assert choice["places_in_band"] == 3
    assert choice["places_selected"] == ["B", "C"]


def test_place_selection_ties_break_on_gmap_id(monkeypatch):
    monkeypatch.setattr(gl, "PLACE_BAND", (5, 5))
    monkeypatch.setattr(gl, "PLACE_CAP", 2)
    choice = gl.select_places(Counter({"B": 5, "A": 5, "C": 5}))
    assert choice["places_selected"] == ["A", "B"]


def test_decoys_are_excluded_for_the_right_reason(tmp_path):
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    places = {row["place_id"] for row in rows}
    assert places == set(COHORT_PLACES)
    # PLACE_TINY is filtered by the degree floors, PLACE_WIDE by the band.
    assert "PLACE_TINY" not in places
    assert "PLACE_WIDE" not in places


def test_whole_place_cohorts_are_kept(tmp_path):
    """No review-level sampling: a selected place keeps all of its text reviews."""
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    for place_id in COHORT_PLACES:
        kept = [row for row in rows if row["place_id"] == place_id]
        assert len(kept) == COHORT_REVIEWERS * REVIEWS_PER_REVIEWER_PER_PLACE


def test_selection_is_deterministic_on_fixture_data(tmp_path):
    review_path, meta_path = build_fixture(tmp_path)
    first, second = tmp_path / "a", tmp_path / "b"
    gl.run(review_path, meta_path, first)
    gl.run(review_path, meta_path, second)
    assert (first / "reviews.csv").read_bytes() == (second / "reviews.csv").read_bytes()


# --- schema mapping and ingestion ---------------------------------------


def test_schema_mapping_produces_the_agreed_fields(tmp_path):
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    assert list(rows[0]) == gl.CSV_FIELDS
    row = rows[0]
    assert row["source"] == "google-local-2021-ucsd"
    assert row["source_url"] == ""
    assert row["country"] == "US"
    assert row["place_id"] in COHORT_PLACES
    assert row["review_id"].startswith("vt21r-")
    assert 1 <= int(row["rating"]) <= 5
    assert row["city"] == "Rutland"
    assert row["region"] == "VT"
    assert row["place_name"] in {"Test Pizza", "Big Grocery"}


def test_coordinate_keys_are_resolved_not_assumed():
    """The dump spells them latitude/longitude; a schema drift must not pass."""
    official = {"gmap_id": "A", "latitude": 1.0, "longitude": 2.0}
    assert gl.resolve_coordinate_keys(official) == ("latitude", "longitude")
    # Older mirrors are tolerated.
    assert gl.resolve_coordinate_keys({"lat": 1.0, "long": 2.0}) == ("lat", "long")
    with pytest.raises(KeyError):
        gl.resolve_coordinate_keys({"gmap_id": "A"})


def test_corpus_without_coordinates_is_refused(tmp_path):
    """A silent coordinate regression must raise, not ship an empty column."""
    review_path, _ = build_fixture(tmp_path)
    meta = [meta_record("PLACE_A"), meta_record("PLACE_B", name="Big Grocery", category=["Grocery store"]),
            meta_record("PLACE_TINY", name="Tiny Cafe", category=["Cafe"]),
            meta_record("PLACE_WIDE", name="Wide Diner", category=["Diner"])]
    for record in meta:
        del record["latitude"], record["longitude"]
    write_gz_jsonl(tmp_path / "meta-Vermont.json.gz", meta)
    with pytest.raises(KeyError):
        gl.run(review_path, tmp_path / "meta-Vermont.json.gz", tmp_path / "out")


def test_coordinates_come_from_business_metadata(tmp_path):
    out_dir = run_fixture(tmp_path)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    assert all(row["latitude"] == "43.61" and row["longitude"] == "-72.97" for row in rows)


def test_rows_load_through_the_production_adapter(tmp_path):
    review_path, meta_path = build_fixture(tmp_path)
    out_dir = tmp_path / "out"
    manifest = gl.run(review_path, meta_path, out_dir)

    ingestion = manifest["ingestion_verification"]
    assert ingestion["imported"] == ingestion["valid"] == 32
    assert ingestion["skipped"] == 0
    assert ingestion["warnings"] == 0

    from reviewscope.ingestion.csv_adapter import CSVAdapter

    result = CSVAdapter().load(out_dir / "reviews.csv")
    reviews = result.reviews
    assert len(reviews) == ingestion["imported"]
    sample = reviews[0]
    assert sample.published_at is not None
    # normalize_review keeps published_at as an ISO string.
    published = datetime.fromisoformat(sample.published_at)
    assert published.year == 2019
    assert published.tzinfo is None
    assert sample.reviewer_id.startswith("vt21u-")
    assert sample.reviewer_name is None
    assert sample.latitude == pytest.approx(43.61)
    assert sample.longitude == pytest.approx(-72.97)
    assert sample.place_category in {"Pizza restaurant", "Grocery store"}
    assert {datetime.fromisoformat(r.published_at).year for r in reviews} == {2019}


def test_stats_report_the_graph_and_coordinate_invariants(tmp_path):
    out_dir = run_fixture(tmp_path)
    stats = json.loads((out_dir / "corpus_stats.json").read_text())

    assert stats["reviews"]["total"] == 32
    assert stats["places"]["total"] == 2
    assert stats["reviewers"]["total"] == COHORT_REVIEWERS
    assert stats["places"]["reviews_per_place"]["median"] == 16.0
    assert stats["reviewers"]["reviews_per_reviewer"]["median"] == 4.0
    assert stats["reviewers"]["distinct_places_per_reviewer"]["median"] == 2.0
    assert stats["reviewers"]["reviewers_with_2plus_places"] == COHORT_REVIEWERS
    assert stats["reviewers"]["reviewers_with_3plus_places"] == 0
    assert stats["categories"]["total"] == 2
    assert stats["graph"]["connected_components"] == 1
    assert stats["graph"]["largest_component_share"] == 1.0
    assert stats["coordinates"]["place_coverage_pct"] == 100.0
    assert stats["coordinates"]["review_coverage_pct"] == 100.0
    assert sum(stats["reviews"]["rating_distribution"].values()) == 32


def test_review_id_is_stable_and_content_derived():
    base = gl.derive_review_id("P", "u", 1000, 5, "text", "fp")
    assert base == gl.derive_review_id("P", "u", 1000, 5, "text", "fp")
    assert base != gl.derive_review_id("P", "u", 1000, 5, "different text", "fp")
    assert base != gl.derive_review_id("P", "u", 2000, 5, "text", "fp")
    assert base != gl.derive_review_id("P", "u", 1000, 5, "text", "other-fp")
    assert base.startswith("vt21r-")


def test_disconnected_history_would_show_as_extra_components():
    """The fixture cohort is connected; a stranded reviewer would split it."""
    edges = [("A", f"u{i}") for i in range(5)] + [("B", f"u{i}") for i in range(5)]
    edges += [("C", "stranded")] * 10
    # 2 places + 5 reviewers form one component; C and "stranded" form another.
    assert gl.connected_components(edges) == [7, 2]


# --- exact duplicate source records --------------------------------------


def test_exact_duplicate_source_record_is_emitted_once(tmp_path):
    """A verbatim repeated record is a release artifact, not a second review."""
    review_path, meta_path = build_fixture(tmp_path)
    raw = [json.loads(line) for line in gzip.open(review_path, "rt", encoding="utf-8")]
    raw.append(dict(raw[0]))
    raw.append(dict(raw[0]))
    write_gz_jsonl(review_path, raw)

    out_dir = tmp_path / "out"
    gl.run(review_path, meta_path, out_dir)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    assert len(rows) == 32, "three identical rows must collapse to one"
    ids = [row["review_id"] for row in rows]
    assert len(set(ids)) == len(ids)

    stats = json.loads((out_dir / "corpus_stats.json").read_text())
    dup = stats["source_duplicates"]
    assert dup["selected_source_rows_before_dedupe"] == 34
    assert dup["exact_duplicate_records_removed"] == 2
    assert dup["unique_canonical_reviews_after_dedupe"] == 32
    assert "sha256" in dup["fingerprint_method"]
    assert dup["rationale"]


def test_dedupe_keeps_the_earliest_occurrence(tmp_path):
    review_path, meta_path = build_fixture(tmp_path)
    raw = [json.loads(line) for line in gzip.open(review_path, "rt", encoding="utf-8")]
    first = raw[0]
    copy = dict(first)
    write_gz_jsonl(review_path, [*raw, copy])

    out_dir = tmp_path / "out"
    gl.run(review_path, meta_path, out_dir)
    rows = load_corpus_csv(out_dir / "reviews.csv")
    expected = gl.derive_review_id(
        first["gmap_id"],
        first["user_id"],
        first["time"],
        first["rating"],
        first["text"],
        gl.canonical_record_fingerprint(first),
    )
    assert expected in {row["review_id"] for row in rows}


def test_dedupe_is_deterministic_across_runs(tmp_path):
    review_path, meta_path = build_fixture(tmp_path)
    raw = [json.loads(line) for line in gzip.open(review_path, "rt", encoding="utf-8")]
    write_gz_jsonl(review_path, [*raw, dict(raw[0]), dict(raw[5])])

    first_dir, second_dir = tmp_path / "a", tmp_path / "b"
    gl.run(review_path, meta_path, first_dir)
    gl.run(review_path, meta_path, second_dir)
    assert (first_dir / "reviews.csv").read_bytes() == (second_dir / "reviews.csv").read_bytes()


def test_review_id_needs_no_source_line_number(tmp_path):
    """After dedupe, identity and content alone identify a review."""
    first = gl.derive_review_id("P", "u", 1000, 5, "text", "fp")
    assert first == gl.derive_review_id("P", "u", 1000, 5, "text", "fp")
    assert first != gl.derive_review_id("P", "u", 2000, 5, "text", "fp")


def test_near_duplicates_are_not_collapsed():
    """Same user, place and time is not enough: every field must match."""
    base = review_record("A", "u")
    variants = {
        "identical": dict(base),
        "other_text": {**base, "text": "different words"},
        "other_rating": {**base, "rating": 1},
        "other_name": {**base, "name": "Someone Else"},
        "other_pics": {**base, "pics": ["photo.jpg"]},
        "other_resp": {**base, "resp": {"text": "thanks"}},
        "other_place": {**base, "gmap_id": "B"},
        "other_reviewer": {**base, "user_id": "v"},
    }
    rows = [
        {"fingerprint": gl.canonical_record_fingerprint(rec), "ordinal": index, "text": rec["text"]}
        for index, rec in enumerate(variants.values())
    ]
    kept, stats = gl.dedupe_exact_duplicates(rows)
    assert stats["exact_duplicate_records_removed"] == 0
    assert len(kept) == len(variants)


def test_distinct_reviews_sharing_user_place_time_survive_end_to_end(tmp_path):
    """An in-cohort near-duplicate is kept; only the verbatim copy is dropped."""
    review_path, meta_path = build_fixture(tmp_path)
    raw = [json.loads(line) for line in gzip.open(review_path, "rt", encoding="utf-8")]
    base = raw[0]
    write_gz_jsonl(
        review_path,
        [
            *raw,
            dict(base),                                   # exact copy: dropped
            {**base, "text": "A different account of it."},
            {**base, "rating": 4},
            {**base, "name": "Somebody Different"},
        ],
    )
    out_dir = tmp_path / "out"
    gl.run(review_path, meta_path, out_dir)

    rows = load_corpus_csv(out_dir / "reviews.csv")
    assert len(rows) == 35, "one exact copy removed, three near-duplicates kept"
    assert len({row["review_id"] for row in rows}) == 35
    assert json.loads((out_dir / "corpus_stats.json").read_text())["source_duplicates"][
        "exact_duplicate_records_removed"
    ] == 1


def test_fingerprint_covers_every_source_field():
    base = review_record("A", "u")
    assert gl.canonical_record_fingerprint(base) == gl.canonical_record_fingerprint(dict(base))
    for field, value in (("name", "Someone Else"), ("pics", ["a.jpg"]), ("resp", {"text": "thanks"})):
        other = dict(base)
        other[field] = value
        assert gl.canonical_record_fingerprint(other) != gl.canonical_record_fingerprint(base)


def test_dedupe_counts_are_reported_in_the_manifest(tmp_path):
    review_path, meta_path = build_fixture(tmp_path)
    raw = [json.loads(line) for line in gzip.open(review_path, "rt", encoding="utf-8")]
    write_gz_jsonl(review_path, [*raw, dict(raw[0])])
    out_dir = tmp_path / "out"
    gl.run(review_path, meta_path, out_dir)

    manifest = json.loads((out_dir / "corpus_manifest.json").read_text())
    dup = manifest["source_review_dedup"]
    assert dup["selected_source_rows_before_dedupe"] == 33
    assert dup["exact_duplicate_records_removed"] == 1
    assert dup["unique_canonical_reviews_after_dedupe"] == 32
    for key in ("fingerprint_method", "rationale", "canonical_occurrence"):
        assert dup[key]


def test_dedupe_exact_duplicates_unit():
    rows = [
        {"fingerprint": "aa", "ordinal": 5, "text": "x"},
        {"fingerprint": "bb", "ordinal": 6, "text": "y"},
        {"fingerprint": "aa", "ordinal": 9, "text": "x"},
    ]
    kept, stats = gl.dedupe_exact_duplicates(rows)
    assert [r["ordinal"] for r in kept] == [5, 6]
    assert stats["exact_duplicate_records_removed"] == 1
    assert stats["unique_canonical_reviews_after_dedupe"] == 2


def test_dense_graph_check_is_reported(tmp_path):
    out_dir = run_fixture(tmp_path)
    check = json.loads((out_dir / "corpus_stats.json").read_text())["dense_graph_check"]
    assert check["clean"] is True
    assert check["violations"] == 0
    assert check["places_below_min_reviews"] == []
    assert check["reviewers_below_min_reviews"] == []
    assert check["reviewers_below_min_places"] == []
    assert check["thresholds"]["min_reviews_per_place"] == 10
    assert check["thresholds"]["min_reviews_per_reviewer"] == 3
    assert check["thresholds"]["min_places_per_reviewer"] == 2


def test_exact_second_still_writes_microseconds():
    """A whole-second source time must keep the microsecond field.

    ``isoformat()`` drops it, and ReviewScope's ``pd.to_datetime`` infers one
    format per column -- so a column mixing ``.123000`` with a bare ``:00``
    coerces the bare rows to NaT and silently drops them from temporal
    analysis. This corpus really contains such rows.
    """
    whole_second = gl.ms_to_published_at(1_556_972_800_000)
    assert whole_second == "2019-05-04T12:26:40.000000+00:00"
    with_fraction = gl.ms_to_published_at(1_556_972_800_123)
    import pandas as pd

    mixed = pd.Series([with_fraction, whole_second])
    assert (
        pd.to_datetime(mixed, errors="coerce").notna().all()
    ), "mixed-precision column must not lose a row"
    assert len({len(p.split(".")[1]) for p in (whole_second, with_fraction)}) == 1
