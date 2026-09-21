"""Streamlit AppTest smoke test (SPEC.md §39-7..§39-20).

Every test runs the real ``app.py`` headless via Streamlit's AppTest
harness and visits each page.  All tests use a *copy* of the production
database (or a synthetic in-memory DB) so the running Streamlit process
is never blocked by a lock on ``data/reviewscope.duckdb``.

Browser is unavailable in this environment, so this is the HTTP/process-level
smoke substitute required by SPEC.md §39 ("if browser unavailable, use
HTTP/process smoke test and record the limitation").
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest

from reviewscope.config import CONFIG
from reviewscope.models.review import NormalizedReview
from reviewscope.storage.duckdb_store import DuckDBStore

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")
PROD_DB = Path(__file__).resolve().parents[1] / "data" / "reviewscope.duckdb"

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from reviewscope.ui import common  # noqa: E402

EMBEDDING_MODEL = CONFIG.embedding.model_name
EMBEDDING_DIM = CONFIG.embedding.dim

_PLACE = "test_place"


# ---------------------------------------------------------------------------
# DB builders
# ---------------------------------------------------------------------------

def _build_db(path: Path, reviews: list[NormalizedReview]) -> str:
    """Write reviews + deterministic fake embeddings into a fresh DuckDB."""
    with DuckDBStore(db_path=path, read_only=False) as store:
        store.ingest(reviews)
        rows = []
        for i, r in enumerate(reviews):
            vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
            vec[i % EMBEDDING_DIM] = 1.0
            rows.append((r.review_id, r.fingerprint(), EMBEDDING_MODEL, vec.tolist()))
        store.store_cached_embeddings(rows)
    return str(path)


def _review(seq: int, **kw) -> NormalizedReview:
    defaults = dict(
        review_id=f"{_PLACE}-{seq:03d}",
        place_id=_PLACE,
        place_name="Test Place",
        place_category="cafe",
        reviewer_id=f"user-{seq % 4:02d}",
        rating=4,
        text=f"Good place to visit on day {seq}. Coffee and pastries.",
        published_at=f"2026-08-{1 + seq % 15:02d}T10:00:00",
        city="Moscow",
        region="Moscow",
        country="Russia",
        latitude=55.75 + seq * 0.0005,
        longitude=37.62 + seq * 0.0005,
    )
    defaults.update(kw)
    return NormalizedReview(**defaults)


_QUIET_TEXTS = [
    "The staff were friendly and the interior was calm in the evening.",
    "Prices are a little high but the portions are generous.",
    "We waited about fifteen minutes for a table on a weekday.",
    "They have great gluten-free options and quick service.",
    "The music was a bit loud and hard to talk over.",
    "Coffee tasted excellent and the pastries were fresh.",
    "The location is easy to find right next to the metro.",
    "Vegetarian dishes were clearly marked on the menu.",
    "Our order arrived correctly and the waiter was polite.",
    "The terrace is lovely when the weather cooperates.",
    "Wi-Fi was slow but the atmosphere made up for it.",
    "Portions are small, so order the larger lunch option.",
    "Payment by card worked fine and the receipt was clear.",
    "The room smelled of fresh bread and spices.",
    "Service slowed down noticeably around eight o'clock when too many people arrived at once.",
]


def _quiet_reviews() -> list[NormalizedReview]:
    return [
        _review(i, rating=[4, 5, 3, 4, 5][i % 5], text=_QUIET_TEXTS[i])
        for i in range(15)
    ]


_PLACE_NC = "no_coord_place"


def _no_coord_reviews() -> list[NormalizedReview]:
    return [
        NormalizedReview(
            review_id=f"{_PLACE_NC}-{i:03d}",
            place_id=_PLACE_NC,
            place_name="No Coords Place",
            place_category="restaurant",
            reviewer_id=f"nc-{i % 3:02d}",
            rating=[4, 3, 5][i % 3],
            text=f"Tasty food on day {i}.",
            published_at=f"2026-09-{1 + i:02d}T10:00:00",
            city="Moscow",
            region="Moscow",
            country="Russia",
        )
        for i in range(12)
    ]


_PLACE_NCAT = "no_cat_place"


def _no_category_reviews() -> list[NormalizedReview]:
    return [
        NormalizedReview(
            review_id=f"{_PLACE_NCAT}-{i:03d}",
            place_id=_PLACE_NCAT,
            place_name="No Category Place",
            place_category=None,
            reviewer_id=f"ncat-{i % 2:02d}",
            rating=[4, 5][i % 2],
            text=f"Interesting place visited on day {i}.",
            published_at=f"2026-08-{20 + i:02d}T12:00:00",
            city="Saint Petersburg",
            region="Saint Petersburg",
            country="Russia",
            latitude=59.93 + i * 0.001,
            longitude=30.31 + i * 0.001,
        )
        for i in range(10)
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def demo_db(tmp_path_factory) -> str:
    if not PROD_DB.exists():
        pytest.skip("demo database not found")
    dst = tmp_path_factory.mktemp("db") / "smoke.duckdb"
    shutil.copy2(PROD_DB, dst)
    return str(dst)


@pytest.fixture(scope="module")
def quiet_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("quiet") / "quiet.duckdb"
    return _build_db(path, _quiet_reviews())


@pytest.fixture(scope="module")
def no_coord_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("nocoord") / "no_coord.duckdb"
    return _build_db(path, _no_coord_reviews())


@pytest.fixture(scope="module")
def no_cat_db(tmp_path_factory) -> str:
    path = tmp_path_factory.mktemp("nocat") / "no_cat.duckdb"
    return _build_db(path, _no_category_reviews())


# ---------------------------------------------------------------------------
# Production demo DB smoke tests
# ---------------------------------------------------------------------------

_PAGE_HEADERS = {
    "Overview": None,  # header is the place name
    "Topics": "Topics",
    "Anomalies": "Anomalies & unusual activity",
    "Duplicates": "Duplicates & repeated text",
    "Reviewers": "Reviewers",
    "Reviewed Places": "Reviewed Places",
    "Data Quality": "Data Quality",
}


def _navigate(at: AppTest, page: str) -> None:
    """Switch the sidebar page radio and rerun (widgets must be re-fetched)."""
    at.sidebar.radio[0].set_value(page)
    at.run()


class TestDemoDBSmoke:
    def test_app_boots_without_exception(self, demo_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", demo_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]

    def test_all_pages_run(self, demo_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", demo_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        assert not list(at.exception)
        for page, header in _PAGE_HEADERS.items():
            _navigate(at, page)
            assert not list(at.exception), (page, [e.value for e in at.exception])
            if header is not None:
                rendered = {h.value for h in at.header}
                assert header in rendered, f"{page!r} did not render header {header!r}"

    def test_overview_shows_rating_metrics(self, demo_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", demo_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        _navigate(at, "Overview")
        metric_values = {m.label: m.value for m in at.metric}
        assert "Raw rating" in metric_values
        assert "Weighted rating" in metric_values
        assert "Reviews" in metric_values

    def test_explainability_rendered(self, demo_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", demo_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        _navigate(at, "Overview")
        md = "\n".join(m.value for m in at.markdown)
        assert "Coordinated activity" in md
        assert "Counter-signals" in md or "+ " in md


# ---------------------------------------------------------------------------
# Synthetic state tests
# ---------------------------------------------------------------------------

class TestSyntheticStates:
    def test_quiet_place_clean_states(self, quiet_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", quiet_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        assert not list(at.exception)

        _navigate(at, "Anomalies")
        info_text = " ".join(i.value for i in at.info)
        assert "No unusual activity detected" in info_text

        _navigate(at, "Duplicates")
        info_text = " ".join(i.value for i in at.info)
        assert "No repeated-text groups" in info_text

        _navigate(at, "Overview")
        metric_values = {m.label: m.value for m in at.metric}
        assert "Raw rating" in metric_values

    def test_missing_coords_states(self, no_coord_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", no_coord_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        assert not list(at.exception)

        _navigate(at, "Reviewed Places")
        info_text = " ".join(i.value for i in at.info)
        assert "No mapped locations" in info_text

        _navigate(at, "Data Quality")
        frames = [d.value for d in at.dataframe if hasattr(d, "value")]
        dq_frame = next((f for f in frames if "Issue" in list(getattr(f, "columns", []))), None)
        assert dq_frame is not None, "Data Quality issue table not found"
        assert (dq_frame["Issue"] == "Missing coordinates").any()

    def test_insufficient_reviewer_history_shows_nodata(self, no_cat_db, monkeypatch) -> None:
        monkeypatch.setattr(common, "DEFAULT_DB_PATH", no_cat_db)
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        _navigate(at, "Reviewers")
        assert not list(at.exception)
        md = "\n".join(m.value for m in at.markdown)
        assert "N/A / insufficient history" in md
