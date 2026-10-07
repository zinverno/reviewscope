"""Phase 16 — dataset discovery layer.

Three layers are covered:

1. the aggregation/caching layer (:mod:`reviewscope.discovery`) — dataset
   totals, per-place evidence, capability detection, filtering, category
   rollups, descriptive rankings;
2. cache behaviour — in-process reuse, on-disk sidecar reuse, and identity-based
   invalidation;
3. the Discover page itself, through Streamlit's ``AppTest`` harness, including
   the drill-down that selects a place and navigates to its Overview.

Browser is unavailable in this environment; ``AppTest`` is the HTTP/process-level
smoke substitute already used by ``tests/test_app_smoke.py`` (SPEC.md §39).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from reviewscope.config import CONFIG
from reviewscope.discovery import (
    DISPLAY_COLUMNS,
    NOT_AVAILABLE,
    SORT_OPTIONS,
    build_dataset_summary,
    category_summary,
    clear_dataset_cache,
    dataset_cache_info,
    dataset_identity,
    display_category_frame,
    display_places_frame,
    filter_places,
    get_dataset_summary,
    ranking_sections,
    source_badge,
)
from reviewscope.models.review import NormalizedReview
from reviewscope.storage.duckdb_store import DuckDBStore

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")
PROD_DB = Path(__file__).resolve().parents[1] / "data" / "reviewscope.duckdb"

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from reviewscope.analysis.engine import AnalysisEngine  # noqa: E402
from reviewscope.ui import common  # noqa: E402

EMBEDDING_MODEL = CONFIG.embedding.model_name
EMBEDDING_DIM = CONFIG.embedding.dim


# ---------------------------------------------------------------------------
# Deterministic fixtures
# ---------------------------------------------------------------------------

#: Four identical reviews → one exact-duplicate group of 4 (duplicate rate 4/7).
ALPHA_DUPLICATE_TEXT = "Отличное место, всем рекомендую! Обслуживание быстрое."

#: Deliberately unlike each other, so no pair clears the duplicate thresholds.
ALPHA_UNIQUE_TEXTS = [
    "Кофе был кислый, заказал два раза и оба раза испортилось.",
    "Музыка играла так громко, что мы не смогли разговаривать.",
    "Официант забыл про наш заказ, ждали двадцать пять минут.",
]

BETA_TEXTS = [
    "Отличный музей, огромная коллекция средневековых картин.",
    "Парковка платная, но экспозиция просторная и тихая.",
    "Детям понравилось, есть интерактивные стенды про космос.",
    "Кафе на первом этаже делает отличный кофе и свежие круассаны.",
]

GAMMA_TEXTS = [
    "Маленькая парикмахерская, стригли быстро и аккуратно.",
    "Цены выше среднего, но качество стрижки хорошее.",
]


def _build_db(path: Path, reviews: list[NormalizedReview]) -> str:
    """Write reviews + deterministic fake embeddings into a fresh DuckDB."""
    with DuckDBStore(db_path=path) as store:
        store.ingest(reviews)
        rows = []
        for i, review in enumerate(reviews):
            vector = np.zeros(EMBEDDING_DIM, dtype=np.float32)
            vector[i % EMBEDDING_DIM] = 1.0
            rows.append((review.review_id, review.fingerprint(), EMBEDDING_MODEL, vector.tolist()))
        store.store_cached_embeddings(rows)
    return str(path)


def _multi_place_reviews(*, dated: bool = False) -> list[NormalizedReview]:
    """Three places in two categories: one with repeats, two clean."""
    reviews: list[NormalizedReview] = []

    def add(
        place_id: str,
        place_name: str,
        category: str,
        rating: int,
        text: str,
        reviewer: str | None = None,
    ) -> None:
        index = len(reviews)
        reviews.append(
            NormalizedReview(
                review_id=f"{place_id}-{index:02d}",
                place_id=place_id,
                place_name=place_name,
                place_category=category,
                reviewer_id=reviewer or f"{place_id}-user-{index}",
                rating=rating,
                text=text,
                published_at=(
                    f"2026-03-{1 + index:02d}T12:00:00" if dated else None
                ),
                city="Moscow" if dated else None,
                region="Moscow" if dated else None,
                country="Russia" if dated else None,
                latitude=55.75 + index * 0.001 if dated else None,
                longitude=37.62 + index * 0.001 if dated else None,
            )
        )

    for _ in range(4):
        add("alpha", "Alpha Cafe", "cafe", 5, ALPHA_DUPLICATE_TEXT)
    for text in ALPHA_UNIQUE_TEXTS:
        add("alpha", "Alpha Cafe", "cafe", 1, text)
    for i, text in enumerate(BETA_TEXTS):
        # one reviewer with history in two places, so the dated fixture also
        # supports reviewer-history analytics
        add(
            "beta",
            "Beta Museum",
            "museum",
            4,
            text,
            reviewer="alpha-user-0" if (dated and i == 0) else None,
        )
    for text in GAMMA_TEXTS:
        add("gamma", "Gamma Salon", "barber", 3, text)
    return reviews


def _textless_reviews() -> list[NormalizedReview]:
    """Reviews without text: text-dependent evidence must be unavailable."""
    return [
        NormalizedReview(
            review_id=f"blank-{i}",
            place_id="blank",
            place_name="Blank Place",
            place_category="cafe",
            reviewer_id=f"blank-user-{i}",
            rating=5,
            text=None,
            published_at=None,
        )
        for i in range(4)
    ]


def _mixed_group_reviews() -> list[NormalizedReview]:
    """One place with a repeated group of 3 and a repeated group of 2.

    The two documented duplicate scopes must be visible in one row: the
    ``duplicate_rate`` counts the group of 3+ only, while ``duplicate_group_count``
    counts both detected groups (the Duplicates page scope).
    """
    reviews: list[NormalizedReview] = []

    def add(index: int, text: str) -> None:
        reviews.append(
            NormalizedReview(
                review_id=f"mixed-{index:02d}",
                place_id="mixed",
                place_name="Mixed Groups",
                place_category="cafe",
                reviewer_id=f"mixed-user-{index}",
                rating=4,
                text=text,
            )
        )

    for index in range(3):
        add(index, "Полностью идентичный отзыв о витрине и кофе, одно и то же.")
    for index in range(3, 5):
        add(index, "Пара identчных отзывов про парковку у входа в центр.")
    add(5, "Совершенно уникальный отзыв про летнюю террасу и музыку.")
    return reviews


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Keep every cache artifact inside tmp_path — never the user cache dir."""
    monkeypatch.setenv("RS_CACHE_DIR", str(tmp_path / "cache"))
    clear_dataset_cache()
    yield
    clear_dataset_cache()


@pytest.fixture(scope="module")
def multi_place_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("discover") / "multi_place.duckdb"
    return _build_db(path, _multi_place_reviews())


@pytest.fixture(scope="module")
def dated_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("discover_dated") / "dated.duckdb"
    return _build_db(path, _multi_place_reviews(dated=True))


@pytest.fixture(scope="module")
def textless_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("discover_blank") / "textless.duckdb"
    return _build_db(path, _textless_reviews())


def _summary(db_path: str):
    store = DuckDBStore(db_path=db_path, read_only=True)
    engine = AnalysisEngine(store)
    return build_dataset_summary(store, engine)


def _row(summary, place_id: str) -> dict:
    return summary.places[summary.places["place_id"] == place_id].iloc[0].to_dict()


# ---------------------------------------------------------------------------
# Dataset summary
# ---------------------------------------------------------------------------


class TestDatasetSummary:
    def test_dataset_totals(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        caps = summary.capabilities
        assert caps.total_reviews == 13
        assert caps.total_places == 3
        assert caps.total_categories == 3
        assert len(summary.places) == 3
        assert summary.places_analyzed == 3
        assert summary.places_failed == 0

    def test_place_rows_carry_existing_evidence(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        alpha = _row(summary, "alpha")
        # four identical reviews inside a 7-review place
        assert alpha["review_count"] == 7
        assert alpha["reviewer_count"] == 7
        assert alpha["duplicate_group_count"] == 1
        assert alpha["largest_duplicate_group"] == 4
        assert alpha["duplicate_rate"] == pytest.approx(4 / 7 * 100, abs=1e-3)
        # production rounds raw/weighted to 2 decimals
        assert alpha["raw_rating"] == pytest.approx(3.29)
        assert alpha["weighted_rating"] == pytest.approx(2.90)
        assert alpha["weighted_rating"] < alpha["raw_rating"]
        assert alpha["abs_rating_delta"] == pytest.approx(
            alpha["rating_delta"], abs=1e-6
        )
        assert alpha["templated_max"] is not None
        assert 0.0 <= alpha["median_specificity"] <= 100.0
        assert alpha["one_star_share"] == pytest.approx(3 / 7 * 100, abs=1e-3)

    def test_rating_delta_equals_the_two_ratings_in_the_same_row(
        self, multi_place_db, dated_db
    ) -> None:
        """A row's delta must be the difference of the ratings it displays.

        The production ``details["delta"]`` is computed before raw/weighted are
        rounded to 2 decimals, so reusing it here made ``Raw - weighted`` differ
        from ``raw - weighted`` of the two printed numbers.
        """
        for db_path in (multi_place_db, dated_db):
            for row in _summary(db_path).places.to_dict(orient="records"):
                raw = row["raw_rating"]
                weighted = row["weighted_rating"]
                delta = row["rating_delta"]
                assert delta == pytest.approx(raw - weighted, abs=1e-6)
                assert row["abs_rating_delta"] == pytest.approx(abs(raw - weighted), abs=1e-6)

    def test_clean_places_have_zero_observed_counts(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        beta = _row(summary, "beta")
        assert beta["duplicate_group_count"] == 0
        assert beta["largest_duplicate_group"] == 0
        assert beta["duplicate_rate"] == pytest.approx(0.0)
        assert summary.places_with_duplicate_groups == 1

    def test_dup_group_count_is_labelled_with_its_own_scope(self, tmp_path) -> None:
        """``Families (2+)`` counts pairs too; the rate counts families of 3+.

        Overview's duplicate rate and the Duplicates page's family count use
        different size floors, so the Discover column states its own scope
        instead of letting the two numbers be read as one metric. The label says
        "families" because a repeated-text family is a connected component, not
        a set of pairwise-similar reviews.
        """
        summary = _summary(_build_db(tmp_path / "mixed.duckdb", _mixed_group_reviews()))
        row = _row(summary, "mixed")
        assert row["duplicate_group_count"] == 2  # group of 3 + group of 2
        assert row["largest_duplicate_group"] == 3
        assert row["duplicate_rate"] == pytest.approx(3 / 6 * 100, abs=1e-3)
        columns = [label for _, label, _ in DISPLAY_COLUMNS]
        assert "Families (2+)" in columns
        assert "Dup groups" not in columns
        assert "Largest group" not in columns
        card = next(s for s in ranking_sections(summary.places) if s.key == "largest_group")
        assert "Families (2+)" in card.frame.columns
        assert "pairs included" in card.description
        # Connected-family semantics are stated, not implied.
        assert "connected component" in card.description
        assert card.title == "Largest repeated-text families"

    def test_place_order_is_deterministic(self, multi_place_db) -> None:
        first = _summary(multi_place_db)
        second = _summary(multi_place_db)
        assert list(first.places["place_name"]) == list(second.places["place_name"])
        assert list(first.places["place_name"]) == ["Alpha Cafe", "Beta Museum", "Gamma Salon"]

    def test_failed_place_is_n_a_not_zero(self, multi_place_db, monkeypatch) -> None:
        store = DuckDBStore(db_path=multi_place_db, read_only=True)
        engine = AnalysisEngine(store)

        def _boom(place_id: str):
            raise RuntimeError("boom")

        monkeypatch.setattr(engine, "analyze", _boom)
        summary = build_dataset_summary(store, engine)
        assert summary.places_failed == 3
        row = _row(summary, "alpha")
        assert pd.isna(row["duplicate_rate"])
        assert pd.isna(row["median_specificity"])
        assert row["analysis_error"] == "RuntimeError: boom"
        assert display_places_frame(summary.places)["Duplicate rate"].tolist() == [
            NOT_AVAILABLE
        ] * 3


# ---------------------------------------------------------------------------
# Capability awareness (missing data is not zero)
# ---------------------------------------------------------------------------


class TestCapabilities:
    def test_temporal_and_reviewer_history_reported_unavailable(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        caps = summary.capabilities
        assert caps.dated_reviews == 0
        assert caps.temporal_available is False
        assert caps.reviewer_history_available is False
        assert caps.coordinated_available is False
        notes = " ".join(caps.notes())
        assert "Temporal anomaly analysis unavailable" in notes
        assert "no publication timestamps" in notes
        assert "Reviewer-history analysis unavailable" in notes

    def test_dated_dataset_enables_temporal_and_coordinated(self, dated_db) -> None:
        summary = _summary(dated_db)
        caps = summary.capabilities
        assert caps.dated_reviews == 13
        assert caps.temporal_available is True
        assert caps.coordinated_available is True
        assert caps.notes() == []
        assert _row(summary, "alpha")["coordinated_score"] is not None

    def test_coordinated_is_never_ranked_without_temporal_evidence(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        assert summary.places["coordinated_score"].isna().all()
        sections = {s.key: s for s in ranking_sections(summary.places)}
        assert sections["coordinated"].available is False
        assert "not ranked" in sections["coordinated"].note

    def test_textless_dataset_still_reports_observed_evidence(self, textless_db) -> None:
        summary = _summary(textless_db)
        row = _row(summary, "blank")
        assert summary.capabilities.text_reviews == 0
        # production still scores empty text, so this is *observed* evidence
        # (all scores at their floor), not missing evidence: no N/A here
        assert row["median_specificity"] == 0.0
        assert row["templated_max"] is not None
        assert row["templated_high_count"] == 0
        display = display_places_frame(summary.places)
        assert display["Max templated"].tolist() != [NOT_AVAILABLE]
        sections = {s.key: s for s in ranking_sections(summary.places)}
        # ... but there is genuinely nothing to rank by topic clusters
        assert sections["topic_clusters"].available is False
        assert sections["topic_clusters"].note
        assert sections["duplicate_rate"].available is True

    def test_unrated_reviews_render_as_not_available(self, tmp_path) -> None:
        reviews = [
            NormalizedReview(
                review_id=f"nr-{i}",
                place_id="norating",
                place_name="No Rating Place",
                place_category="cafe",
                reviewer_id=f"nr-user-{i}",
                rating=None,
                text=f"Отзыв без оценки номер {i} про разные детали витрины.",
                published_at=None,
            )
            for i in range(3)
        ]
        summary = _summary(_build_db(tmp_path / "norating.duckdb", reviews))
        display = display_places_frame(summary.places)
        assert display["Raw rating"].tolist() == [NOT_AVAILABLE]
        assert display["1★ share"].tolist() == [NOT_AVAILABLE]
        sections = {s.key: s for s in ranking_sections(summary.places)}
        assert sections["rating_delta"].available is False
        assert sections["one_star"].available is False


# ---------------------------------------------------------------------------
# Filtering / sorting
# ---------------------------------------------------------------------------


class TestFiltering:
    def test_category_filter(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        assert len(filter_places(summary.places, categories=("cafe",))) == 1
        assert len(filter_places(summary.places, categories=("cafe", "museum"))) == 2
        assert len(filter_places(summary.places)) == 3

    def test_minimum_review_filter(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        assert list(filter_places(summary.places, min_reviews=4)["place_name"]) == [
            "Alpha Cafe",
            "Beta Museum",
        ]
        assert list(filter_places(summary.places, min_reviews=7)["place_name"]) == ["Alpha Cafe"]

    def test_name_search(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        assert list(filter_places(summary.places, query="museum")["place_name"]) == ["Beta Museum"]
        assert len(filter_places(summary.places, query="a")) == 3  # substring, case-insensitive

    def test_duplicate_rate_range(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        assert list(filter_places(summary.places, duplicate_rate_range=(50.0, 100.0))["place_name"]) == [
            "Alpha Cafe"
        ]
        assert list(filter_places(summary.places, duplicate_rate_range=(0.0, 1.0))["place_name"]) == [
            "Beta Museum",
            "Gamma Salon",
        ]

    def test_sorting_is_deterministic_in_both_directions(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        high = list(filter_places(summary.places, sort_by="Reviews", descending=True)["place_name"])
        low = list(filter_places(summary.places, sort_by="Reviews", descending=False)["place_name"])
        assert high[0] == "Alpha Cafe"
        assert low[-1] == "Alpha Cafe"
        assert sorted(high) == sorted(low)
        assert list(filter_places(summary.places, sort_by="Median specificity", descending=True)["place_name"])[0] == (
            "Beta Museum"
        )

    def test_sort_options_resolve_to_real_columns(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        for label, column in SORT_OPTIONS.items():
            assert column in summary.places.columns, label
            assert len(filter_places(summary.places, sort_by=label)) == 3

    def test_filters_combine_and_can_exclude_everything(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        empty = filter_places(summary.places, categories=("barber",), min_reviews=99)
        assert empty.empty
        assert display_places_frame(empty).empty

    def test_empty_frame_is_handled(self) -> None:
        empty = pd.DataFrame()
        assert filter_places(empty).empty
        assert ranking_sections(empty) == []
        assert display_places_frame(empty).empty
        assert display_category_frame(category_summary(empty)).empty


# ---------------------------------------------------------------------------
# Category rollup
# ---------------------------------------------------------------------------


class TestCategorySummary:
    def test_category_medians_and_quartiles(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        frame = summary.categories.set_index("place_category")
        assert set(frame.index) == {"barber", "cafe", "museum"}
        assert frame.loc["cafe", "places"] == 1
        assert frame.loc["cafe", "reviews"] == 7
        assert frame.loc["museum", "reviews"] == 4
        assert frame.loc["cafe", "median_raw_rating"] == pytest.approx(3.29)
        assert frame.loc["cafe", "q1_raw_rating"] is not None
        assert frame.loc["museum", "median_duplicate_rate"] == pytest.approx(0.0)

    def test_category_rollup_has_no_suspicion_score(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        forbidden = ("score", "suspicion", "risk", "fraud", "verdict")
        for column in summary.categories.columns:
            assert not any(word in column.lower() for word in forbidden), column

    def test_missing_category_bucketed(self, tmp_path) -> None:
        reviews = [
            review.model_copy(update={"place_category": None})
            for review in _multi_place_reviews()[:4]
        ]
        summary = _summary(_build_db(tmp_path / "nocat.duckdb", reviews))
        assert list(summary.categories["place_category"]) == ["(no category)"]
        assert summary.capabilities.total_categories == 0

    def test_display_marks_unavailable(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        display = display_category_frame(summary.categories)
        assert display["Median raw rating"].tolist()[0] != NOT_AVAILABLE
        assert "Median raw rating" in display.columns


# ---------------------------------------------------------------------------
# Ranking views
# ---------------------------------------------------------------------------


class TestRankings:
    def test_sections_cover_the_discovery_questions(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        keys = [s.key for s in ranking_sections(summary.places)]
        assert keys == [
            "duplicate_rate",
            "largest_group",
            "rating_delta",
            "high_specificity",
            "low_specificity",
            "topic_clusters",
            "templated",
            "cohort",
            "one_star",
            "five_star",
            "coordinated",
        ]

    def test_duplicate_rate_ranking_is_descriptive(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        section = next(s for s in ranking_sections(summary.places) if s.key == "duplicate_rate")
        assert section.frame["Place"].tolist() == ["Alpha Cafe", "Beta Museum", "Gamma Salon"]
        assert section.frame["Duplicate rate"].tolist()[0] == "57.1%"

    def test_lowest_specificity_ranks_the_other_end(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        sections = {s.key: s for s in ranking_sections(summary.places)}
        high = sections["high_specificity"].frame["Place"].tolist()
        low = sections["low_specificity"].frame["Place"].tolist()
        assert high[0] != low[0] or len(set(high)) == 1

    def test_rankings_follow_the_filtered_frame(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        filtered = filter_places(summary.places, categories=("cafe",))
        section = next(s for s in ranking_sections(filtered) if s.key == "cohort")
        assert section.frame["Place"].tolist() == ["Alpha Cafe"]

    def test_unavailable_sections_carry_a_note_instead_of_a_table(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        section = next(s for s in ranking_sections(summary.places) if s.key == "topic_clusters")
        assert section.available is False
        assert section.frame.empty
        assert section.note

    def test_no_verdict_wording_anywhere(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        forbidden = ("fake", "fraud", "manipulat", "scam", "suspicious", "cheat")
        blob = " ".join(
            s.title + " " + s.description + " " + s.note for s in ranking_sections(summary.places)
        ).lower()
        for word in forbidden:
            assert word not in blob, word

    def test_top_n_respected(self, multi_place_db) -> None:
        summary = _summary(multi_place_db)
        section = next(s for s in ranking_sections(summary.places, top_n=2) if s.key == "cohort")
        assert len(section.frame) == 2


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestDatasetCache:
    def test_memory_cache_avoids_rebuild(self, multi_place_db) -> None:
        store = DuckDBStore(db_path=multi_place_db, read_only=True)
        engine = AnalysisEngine(store)
        first = get_dataset_summary(multi_place_db, store, engine)
        second = get_dataset_summary(multi_place_db, store, engine)
        assert first.source == "live"
        assert second.source == "memory"
        assert dataset_cache_info()["live_builds"] == 1
        assert dataset_cache_info()["memory_hits"] == 1
        assert list(second.places["place_name"]) == list(first.places["place_name"])

    def test_disk_sidecar_survives_a_fresh_cache(self, multi_place_db) -> None:
        store = DuckDBStore(db_path=multi_place_db, read_only=True)
        engine = AnalysisEngine(store)
        live = get_dataset_summary(multi_place_db, store, engine)
        clear_dataset_cache()
        restored = get_dataset_summary(multi_place_db, store, engine)
        assert restored.source == "disk"
        # counters were reset by clear_dataset_cache(); a disk hit must not
        # count as a live build
        assert dataset_cache_info()["live_builds"] == 0
        assert dataset_cache_info()["disk_hits"] == 1
        assert restored.build_seconds == live.build_seconds
        pd.testing.assert_frame_equal(
            live.places.reset_index(drop=True), restored.places.reset_index(drop=True)
        )
        pd.testing.assert_frame_equal(
            live.categories.reset_index(drop=True), restored.categories.reset_index(drop=True)
        )
        assert source_badge(restored.source) == "reused on-disk cache (warm)"

    def test_identity_depends_on_the_dataset_file(self, tmp_path) -> None:
        first = _build_db(tmp_path / "a.duckdb", _textless_reviews()[:2])
        second = _build_db(tmp_path / "b.duckdb", _textless_reviews()[:3])
        assert dataset_identity(first) != dataset_identity(second)
        assert dataset_identity(first) == dataset_identity(first)
        assert dataset_identity(Path(first).name) != dataset_identity(first)

    def test_rebuilt_dataset_is_not_served_from_a_stale_cache(self, tmp_path) -> None:
        path = tmp_path / "growing.duckdb"
        db_path = _build_db(path, _textless_reviews()[:2])
        store = DuckDBStore(db_path=db_path, read_only=True)
        engine = AnalysisEngine(store)
        first = get_dataset_summary(db_path, store, engine)
        assert first.capabilities.total_reviews == 2
        store.close()

        # ingest more reviews into the same file → new identity → new summary
        writable = DuckDBStore(db_path=db_path)
        writable.ingest(_textless_reviews()[2:])
        writable.close()
        store2 = DuckDBStore(db_path=db_path, read_only=True)
        engine2 = AnalysisEngine(store2)
        second = get_dataset_summary(db_path, store2, engine2)
        assert second.capabilities.total_reviews == 4
        assert second.source == "live"

    def test_clear_dataset_cache_drops_entries(self, multi_place_db) -> None:
        store = DuckDBStore(db_path=multi_place_db, read_only=True)
        engine = AnalysisEngine(store)
        get_dataset_summary(multi_place_db, store, engine)
        assert dataset_cache_info()["entries"] == 1
        clear_dataset_cache()
        assert dataset_cache_info()["entries"] == 0


# ---------------------------------------------------------------------------
# AppTest — the Discover page
# ---------------------------------------------------------------------------


def _discover(db_path: str, monkeypatch) -> AppTest:
    monkeypatch.setattr(common, "DEFAULT_DB_PATH", db_path)
    at = AppTest.from_file(APP_PATH, default_timeout=300)
    at.run()
    assert not list(at.exception), [e.value for e in at.exception]
    at.sidebar.radio[0].set_value("Discover")
    at.run()
    assert not list(at.exception), [e.value for e in at.exception]
    return at


# Rendered column signature of every ranking card. An unavailable ranking
# renders no table at all, so a missing signature is a meaningful assertion.
RANKING_COLUMNS: dict[str, tuple[str, ...]] = {
    "duplicate_rate": ("Place", "Category", "Reviews", "Duplicate rate", "Largest family"),
    "largest_group": ("Place", "Category", "Largest family", "Families (2+)", "Reviews"),
    "rating_delta": ("Place", "Category", "Reviews", "Raw rating", "Weighted rating", "Raw − weighted"),
    "high_specificity": ("Place", "Category", "Reviews", "Median specificity", "Mean specificity"),
    "low_specificity": ("Place", "Category", "Reviews", "Median specificity"),
    "topic_clusters": ("Place", "Category", "Reviews", "Topic clusters"),
    "templated": ("Place", "Category", "Reviews", "Max templated", "Median templated", "≥65"),
    "cohort": ("Place", "Category", "Reviews", "Reviewers"),
    "one_star": ("Place", "Category", "Reviews", "1★ share", "Raw rating"),
    "five_star": ("Place", "Category", "Reviews", "5★ share", "Raw rating"),
    "coordinated": ("Place", "Category", "Reviews", "Coordinated", "Confidence"),
}


def _widget(at: AppTest, kind: str, key: str):
    return getattr(at, kind)(key=key)


def _places_table(at: AppTest):
    for frame in (d.value for d in at.dataframe):
        columns = list(getattr(frame, "columns", []))
        if columns[:2] == ["Place", "Category"] and len(columns) > 5:
            return frame
    raise AssertionError("places comparison table not found")


def _section_frame(at: AppTest, key: str):
    expected = RANKING_COLUMNS[key]
    for frame in (d.value for d in at.dataframe):
        if tuple(getattr(frame, "columns", ())) == expected:
            return frame
    return None


class TestDiscoverApp:
    def test_page_boots_and_is_navigable(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        assert "Discover" in {h.value for h in at.header}
        assert not list(at.exception)
        assert not list(at.warning)
        # place-level filters still work on the other pages
        at.sidebar.radio[0].set_value("Topics")
        at.run()
        assert not list(at.exception)
        assert "Topics" in {h.value for h in at.header}

    def test_dataset_summary_metrics(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        metrics = {m.label: m.value for m in at.metric}
        assert metrics["Reviews"] == "13"
        assert metrics["Places"] == "3"
        assert metrics["Categories"] == "3"
        assert metrics["Places with repeated-text families"] == "1"
        assert metrics["Places with topic clusters"] == "0"
        captions = " ".join(c.value for c in at.caption)
        assert "Dataset summary:" in captions
        assert "places analysed" in captions
        table = _places_table(at)
        assert len(table) == 3
        assert set(table["Place"]) == {"Alpha Cafe", "Beta Museum", "Gamma Salon"}
        assert "57.1%" in table["Duplicate rate"].tolist()
        assert table["Coordinated"].tolist() == [NOT_AVAILABLE] * 3

    def test_category_filter(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "multiselect", "discover_category_filter").set_value(["museum"])
        at.run()
        assert not list(at.exception)
        table = _places_table(at)
        assert list(table["Place"]) == ["Beta Museum"]
        assert "1 of 3 places shown" in " ".join(c.value for c in at.caption)

    def test_minimum_review_filter(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "number_input", "discover_min_reviews").set_value(4)
        at.run()
        assert not list(at.exception)
        assert set(_places_table(at)["Place"]) == {"Alpha Cafe", "Beta Museum"}

    def test_name_search_filter(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "text_input", "discover_query").set_value("gamma")
        at.run()
        assert not list(at.exception)
        assert list(_places_table(at)["Place"]) == ["Gamma Salon"]

    def test_duplicate_rate_range_filter(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "slider", "discover_dup_rate_range").set_value((50.0, 100.0))
        at.run()
        assert not list(at.exception)
        assert list(_places_table(at)["Place"]) == ["Alpha Cafe"]

    def test_sorting(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        table = _places_table(at)
        assert list(table["Reviews"])[0] == "7"
        _widget(at, "selectbox", "discover_sort_by").set_value("Median specificity")
        _widget(at, "toggle", "discover_sort_order").set_value(False)
        at.run()
        assert not list(at.exception)
        ordered = list(_places_table(at)["Place"])
        # ascending specificity: Alpha (35.0) < Gamma (43.8) < Beta (55.0)
        assert ordered == ["Alpha Cafe", "Gamma Salon", "Beta Museum"]
        assert "lowest first" in " ".join(c.value for c in at.caption)

    def test_no_places_match_filters_state(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "multiselect", "discover_category_filter").set_value(["barber"])
        # the widget caps at the largest cohort (7 reviews), so 5 leaves
        # the only barber place (2 reviews) filtered out
        _widget(at, "number_input", "discover_min_reviews").set_value(5)
        at.run()
        assert not list(at.exception)
        assert "No places match the current filters" in " ".join(i.value for i in at.info)
        assert "0 of 3 places shown" in " ".join(c.value for c in at.caption)

    def test_unavailable_temporal_metrics_are_stated(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        info = " ".join(i.value for i in at.info)
        assert "Temporal anomaly analysis unavailable" in info
        assert "no publication timestamps" in info
        assert "Reviewer-history analysis unavailable" in info
        # the coordinated ranking must be replaced by an explanation, not a table
        assert _section_frame(at, "coordinated") is None
        assert "not ranked" in "\n".join(c.value for c in at.caption) + info
        assert _places_table(at)["Coordinated"].tolist() == [NOT_AVAILABLE] * 3

    def test_temporal_dataset_reports_coordinated_scores(self, dated_db, monkeypatch) -> None:
        at = _discover(dated_db, monkeypatch)
        assert not [i for i in at.info if "Temporal anomaly" in i.value]
        table = _places_table(at)
        assert all(v != NOT_AVAILABLE for v in table["Coordinated"])
        assert _section_frame(at, "coordinated") is not None

    def test_rankings_render(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        duplicate_frame = _section_frame(at, "duplicate_rate")
        assert duplicate_frame is not None
        assert duplicate_frame["Place"].tolist() == ["Alpha Cafe", "Beta Museum", "Gamma Salon"]
        assert "Most semantic topic clusters" in "\n".join(m.value for m in at.markdown)
        # category comparison table
        category_tables = [
            d.value for d in at.dataframe if "Median duplicate rate" in list(getattr(d.value, "columns", []))
        ]
        assert category_tables, "category comparison table not found"
        assert set(category_tables[0]["Category"]) == {"barber", "cafe", "museum"}

    def test_select_place_and_open_overview(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "selectbox", "discover_focus_place").set_value("gamma")
        at.run()
        assert not list(at.exception)
        _widget(at, "button", "discover_open_overview").click()
        at.run()
        assert not list(at.exception)
        assert at.sidebar.radio[0].value == "Overview"
        assert at.sidebar.selectbox[0].value == "gamma"
        assert "Gamma Salon" in {h.value for h in at.header}
        metrics = {m.label: m.value for m in at.metric}
        assert metrics["Reviews"] == "2"

    def test_drill_down_shortcuts(self, multi_place_db, monkeypatch) -> None:
        for button_key, page, header in (
            ("discover_open_duplicates", "Duplicates", "Repeated-text families"),
            ("discover_open_topics", "Topics", "Topics"),
        ):
            at = _discover(multi_place_db, monkeypatch)
            _widget(at, "selectbox", "discover_focus_place").set_value("beta")
            at.run()
            _widget(at, "button", button_key).click()
            at.run()
            assert not list(at.exception)
            assert at.sidebar.radio[0].value == page
            assert at.sidebar.selectbox[0].value == "beta"
            assert header in {h.value for h in at.header}

    def test_selection_persists_after_drill_down(self, multi_place_db, monkeypatch) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "selectbox", "discover_focus_place").set_value("beta")
        at.run()
        _widget(at, "button", "discover_open_overview").click()
        at.run()
        assert at.sidebar.selectbox[0].value == "beta"

        for page in ("Topics", "Anomalies", "Duplicates", "Reviewers", "Discover"):
            at.sidebar.radio[0].set_value(page)
            at.run()
            assert not list(at.exception), page
            assert at.sidebar.selectbox[0].value == "beta", f"selection lost on {page}"
        at.sidebar.radio[0].set_value("Overview")
        at.run()
        assert "Beta Museum" in {h.value for h in at.header}

    def test_focus_selectbox_resets_when_filters_exclude_the_place(
        self, multi_place_db, monkeypatch
    ) -> None:
        at = _discover(multi_place_db, monkeypatch)
        _widget(at, "selectbox", "discover_focus_place").set_value("gamma")
        at.run()
        assert not list(at.exception)
        _widget(at, "text_input", "discover_query").set_value("beta")
        at.run()
        assert not list(at.exception)
        assert _widget(at, "selectbox", "discover_focus_place").value == "beta"

    def test_production_demo_dataset_boot(self, monkeypatch, tmp_path) -> None:
        if not PROD_DB.exists():
            pytest.skip("demo database not found")
        copy = tmp_path / "demo.duckdb"
        shutil.copy2(PROD_DB, copy)
        at = _discover(str(copy), monkeypatch)
        assert not list(at.exception)
        metrics = {m.label: m.value for m in at.metric}
        assert int(metrics["Places"]) > 1
        assert len(_places_table(at)) == int(metrics["Places"])


# ---------------------------------------------------------------------------
# Empty dataset
# ---------------------------------------------------------------------------


class TestEmptyDataset:
    def test_summary_of_a_dataset_without_reviews(self, tmp_path) -> None:
        path = tmp_path / "empty.duckdb"
        with DuckDBStore(db_path=path):
            pass
        summary = _summary(str(path))
        assert summary.capabilities.total_reviews == 0
        assert summary.places.empty
        assert summary.categories.empty
        assert summary.places_analyzed == 0
        assert display_places_frame(summary.places).empty
        assert ranking_sections(summary.places) == []
