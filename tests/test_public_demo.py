"""Public-demo mode regression tests (Phase 19).

Covers the deployment restrictions: fixed packaged dataset, no path input,
read-only store, no visitor-triggered filesystem writes, annotation tooling
disabled, packaged artifact integrity, and safe rendering of review text.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from reviewscope.config import CONFIG
from reviewscope.embeddings.base import embedding_key
from reviewscope.embeddings.sentence_transformer import SentenceTransformerProvider
from reviewscope.models.review import NormalizedReview
from reviewscope.public_demo import (
    ENV_DB_PATH,
    ENV_FLAG,
    configure_public_demo,
    missing_embedding_keys,
    public_demo_db_path,
    public_demo_enabled,
)
from reviewscope.storage import DuckDBStore

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = str(REPO_ROOT / "app.py")
APP_LABELING_PATH = str(REPO_ROOT / "app_labeling.py")
ARTIFACT = REPO_ROOT / "demo" / "reviewscope_demo.duckdb"
MANIFEST = REPO_ROOT / "demo" / "manifest.json"

streamlit = pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

from reviewscope.discovery.summary import (  # noqa: E402
    clear_dataset_cache,
    dataset_cache_info,
)

ENCODING_ID = SentenceTransformerProvider().contract.encoding_id
EMBEDDING_DIM = CONFIG.embedding.dim


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_script = _load_script("build_public_demo_dataset")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _review(seq: int, **kw) -> NormalizedReview:
    defaults = dict(
        review_id=f"hp-{seq:03d}",
        place_id="hostile",
        place_name="Hostile Text Café",
        place_category="coffee",
        reviewer_id=f"rv-{seq:03d}",
        reviewer_name=f"Reviewer {seq}",
        rating=4,
        text="Plain review text.",
        published_at="2026-03-01T10:00:00",
        city="Testopolis",
        region="Test",
        country="TC",
        latitude=1.0,
        longitude=2.0,
        source="demo",
        source_url="https://demo.example/x",
    )
    defaults.update(kw)
    return NormalizedReview(**defaults)


def _build_demo_artifact(path: Path, reviews: list[NormalizedReview]) -> str:
    """Packaged-artifact shaped temp dataset: reviews + fake embeddings."""
    with DuckDBStore(db_path=path, read_only=False) as store:
        store.ingest(reviews)
        rows = []
        for i, review in enumerate(reviews):
            vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
            vec[i % EMBEDDING_DIM] = 1.0
            rows.append((*embedding_key(review.review_id, review.text_or_empty(), ENCODING_ID), vec.tolist()))
        store.store_cached_embeddings(rows)
    return str(path)


def _rendered(at: AppTest) -> str:
    parts = [m.value for m in at.markdown]
    parts += [c.value for c in at.caption]
    parts += [h.value for h in at.header]
    parts += [s.value for s in at.subheader]
    parts += [i.value for i in at.info]
    parts += [e.value for e in at.error]
    return "\n".join(parts)


def _sidebar_text_input_labels(at: AppTest) -> list[str]:
    return [w.label for w in at.sidebar.text_input]


# ---------------------------------------------------------------------------
# Configuration unit tests
# ---------------------------------------------------------------------------


class TestFlag:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv(ENV_FLAG, value)
        assert public_demo_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe"])
    def test_falsy(self, monkeypatch, value: str) -> None:
        monkeypatch.setenv(ENV_FLAG, value)
        assert public_demo_enabled() is False

    def test_default_off(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        assert public_demo_enabled() is False

    def test_secret_fallback(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        monkeypatch.setattr("reviewscope.public_demo._secret", lambda name: "1")
        assert public_demo_enabled() is True

    def test_env_wins_over_secret(self, monkeypatch) -> None:
        monkeypatch.setenv(ENV_FLAG, "0")
        monkeypatch.setattr("reviewscope.public_demo._secret", lambda name: "1")
        assert public_demo_enabled() is False


class TestPathsAndConfig:
    def test_env_db_path_override(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv(ENV_DB_PATH, str(tmp_path / "other.duckdb"))
        assert public_demo_db_path() == tmp_path / "other.duckdb"

    def test_default_path_points_at_packaged_artifact(self, monkeypatch) -> None:
        monkeypatch.delenv(ENV_DB_PATH, raising=False)
        candidate = public_demo_db_path()
        assert str(candidate).endswith("demo/reviewscope_demo.duckdb")

    def test_configure_forces_disk_cache_off(self, monkeypatch) -> None:
        monkeypatch.setenv("RS_DISCOVERY_DISK_CACHE", "1")
        configure_public_demo()
        assert os.environ["RS_DISCOVERY_DISK_CACHE"] == "0"


# ---------------------------------------------------------------------------
# Packaged artifact integrity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not ARTIFACT.exists(), reason="packaged demo artifact missing")
class TestPackagedArtifact:
    def test_all_frozen_vectors_without_optional_imports(self):
        script = """
import sys
class BlockBackend:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'torch', 'sentence_transformers', 'transformers'}:
            raise AssertionError(f'unexpected backend import {fullname}')
sys.meta_path.insert(0, BlockBackend())
import numpy as np
from reviewscope.embeddings.cache import EmbeddingCache
from reviewscope.embeddings.frozen_demo import FROZEN_ID
from reviewscope.storage import DuckDBStore
from reviewscope.public_demo import missing_embedding_keys
with DuckDBStore(sys.argv[1], read_only=True) as store:
    reviews = store.fetch_reviews()
    cache = EmbeddingCache(store)
    assert cache.encoding_id == FROZEN_ID and not missing_embedding_keys(store, reviews)
    vectors = cache.embed_reviews(reviews)
    assert vectors.shape == (1058, 384) and vectors.dtype == np.float32
    historical = store.get_cached_embeddings([
        (r.review_id, r.fingerprint(), cache.provider_name) for r in reviews
    ])
    for r, vec in zip(reviews, vectors, strict=True):
        np.testing.assert_array_equal(vec, historical[(r.review_id, r.fingerprint(), cache.provider_name)])
    changed = reviews[0].model_copy(update={'text': ' ' + reviews[0].text})
    assert changed.fingerprint() == reviews[0].fingerprint()
    assert cache.cache_hit_ratio([changed]) == 0
    try:
        cache.embed_reviews([changed])
    except ValueError as exc:
        assert 'precomputed embeddings' in str(exc)
    else:
        raise AssertionError('modified text admitted to frozen snapshot')
"""
        before = _sha256(ARTIFACT)
        subprocess.run([sys.executable, "-c", script, str(ARTIFACT)], check=True,
                       env={**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")})
        assert _sha256(ARTIFACT) == before

    def test_explicit_or_changed_model_cannot_claim_frozen_vectors(self, monkeypatch):
        from reviewscope.embeddings import EmbeddingCache

        with DuckDBStore(ARTIFACT, read_only=True) as store:
            reviews = store.fetch_reviews()
            explicit = EmbeddingCache(store, SentenceTransformerProvider())
            assert explicit.cache_hit_ratio(reviews) == 0
            changed = replace(CONFIG, embedding=replace(CONFIG.embedding, revision="a" * 40))
            monkeypatch.setattr("reviewscope.embeddings.sentence_transformer.CONFIG", changed)
            assert EmbeddingCache(store).cache_hit_ratio(reviews) == 0

    @pytest.mark.parametrize("change", [
        "UPDATE reviews SET text = ' ' || text WHERE review_id = (SELECT min(review_id) FROM reviews)",
        "UPDATE embeddings_cache SET embedding = list_reverse(embedding) WHERE review_id = (SELECT min(review_id) FROM reviews)",
    ])
    def test_modified_legacy_snapshot_is_ineligible(self, tmp_path, change):
        copy = tmp_path / "altered.duckdb"
        shutil.copyfile(ARTIFACT, copy)
        with DuckDBStore(copy) as store:
            store.connection().execute(change)
        with DuckDBStore(copy, read_only=True) as store:
            reviews = store.fetch_reviews()
            assert len(missing_embedding_keys(store, reviews)) == len(reviews)

    def test_manifest_matches_artifact(self) -> None:
        assert build_script.verify(ARTIFACT) == []

    def test_preflight_finds_no_missing_embeddings(self) -> None:
        with DuckDBStore(db_path=ARTIFACT, read_only=True) as store:
            reviews = store.fetch_reviews()
            assert reviews
            assert missing_embedding_keys(store, reviews) == []

    def test_only_synthetic_source_rows(self) -> None:
        with DuckDBStore(db_path=ARTIFACT, read_only=True) as store:
            rows = store.connection().execute(
                "SELECT DISTINCT source FROM reviews"
            ).fetchall()
        assert {r[0] for r in rows} == {"demo"}


# ---------------------------------------------------------------------------
# Read-only guarantees
# ---------------------------------------------------------------------------


class TestReadOnlyStore:
    def test_missing_file_is_not_created(self, tmp_path) -> None:
        target = tmp_path / "absent.duckdb"
        with pytest.raises(FileNotFoundError):
            DuckDBStore(db_path=target, read_only=True)
        assert not target.exists()

    def test_embedding_writes_are_skipped(self, tmp_path) -> None:
        path = tmp_path / "ro.duckdb"
        _build_demo_artifact(path, [_review(1)])
        before = _sha256(path)
        with DuckDBStore(db_path=path, read_only=True) as store:
            assert store.store_cached_embeddings(
                [("x", "y", ENCODING_ID, [0.0] * 4)]
            ) == 0
            with pytest.raises(AssertionError):
                store.ingest([_review(2)])
        assert _sha256(path) == before

    def test_readonly_connection_reports_itself(self, tmp_path) -> None:
        path = tmp_path / "ro2.duckdb"
        _build_demo_artifact(path, [_review(1)])
        with DuckDBStore(db_path=path, read_only=True) as store:
            assert store.read_only is True
            assert store.review_count() == 1


# ---------------------------------------------------------------------------
# Public-demo app behaviour
# ---------------------------------------------------------------------------


@pytest.fixture
def demo_env(monkeypatch, tmp_path):
    """Public-demo mode with caches confined to the test's tmp dir."""
    monkeypatch.setenv(ENV_FLAG, "1")
    # The app forces this to "0"; pre-setting it lets monkeypatch restore it.
    monkeypatch.setenv("RS_DISCOVERY_DISK_CACHE", "1")
    monkeypatch.setenv("RS_CACHE_DIR", str(tmp_path / "cache"))
    clear_dataset_cache()
    yield tmp_path
    clear_dataset_cache()


class TestPublicDemoApp:
    @pytest.mark.skipif(not ARTIFACT.exists(), reason="packaged demo artifact missing")
    def test_boots_packaged_dataset_without_path_input(self, demo_env) -> None:
        before = _sha256(ARTIFACT)
        at = AppTest.from_file(APP_PATH, default_timeout=300)
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]
        # No filesystem path entry point for visitors.
        assert _sidebar_text_input_labels(at) == []
        assert not list(at.text_input)
        # Synthetic-data disclosure is present on the page and in the sidebar.
        assert "Public demo — synthetic data" in _rendered(at)
        assert any("Synthetic demo data" in c.value for c in at.caption)
        assert "does not assert fraud" in _rendered(at)
        # The packaged dataset is what the sidebar selected.
        assert len(at.sidebar.selectbox[0].options) >= 1
        assert _sha256(ARTIFACT) == before, "demo run modified the packaged artifact"

    @pytest.mark.skipif(not ARTIFACT.exists(), reason="packaged demo artifact missing")
    def test_all_pages_render(self, demo_env) -> None:
        at = AppTest.from_file(APP_PATH, default_timeout=300)
        at.run()
        assert not list(at.exception)
        headers = {
            "Discover": "Discover",
            "Topics": "Topics",
            "Anomalies": "Anomalies & unusual activity",
            "Duplicates": "Repeated-text families",
            "Reviewers": "Reviewers",
            "Reviewed Places": "Reviewed Places",
            "Data Quality": "Data Quality",
        }
        for page, header in headers.items():
            at.sidebar.radio[0].set_value(page)
            at.run()
            assert not list(at.exception), (page, [e.value for e in at.exception])
            assert header in {h.value for h in at.header}, page

    def test_local_mode_keeps_dataset_picker(self, demo_env, monkeypatch, tmp_path) -> None:
        """Removing the picker is a demo-mode change only."""
        path = tmp_path / "local.duckdb"
        _build_demo_artifact(path, [_review(i) for i in range(1, 4)])
        monkeypatch.setenv(ENV_FLAG, "0")
        monkeypatch.setattr("reviewscope.ui.common.DEFAULT_DB_PATH", str(path))
        at = AppTest.from_file(APP_PATH, default_timeout=120)
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]
        assert "Dataset (duckdb path)" in _sidebar_text_input_labels(at)

    def test_no_disk_sidecar_written(self, demo_env, monkeypatch, tmp_path) -> None:
        path = tmp_path / "hostile.duckdb"
        reviews = [
            _review(1, text="Nice quiet place, would return."),
            _review(2, text="Second opinion about the coffee."),
            _review(3, reviewer_id="rv-9", text="Third opinion about the terrace."),
        ]
        _build_demo_artifact(path, reviews)
        monkeypatch.setenv(ENV_DB_PATH, str(path))
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        at.sidebar.radio[0].set_value("Discover")
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]
        assert dataset_cache_info()["disk_writes"] == 0
        cache_dir = tmp_path / "cache"
        assert not cache_dir.exists() or not any(cache_dir.rglob("*.json"))


class TestHostileReviewText:
    """Untrusted review text must never re-shape the page (markdown injection)."""

    EVIL = (
        "# EVIL_HEADING impersonates a header\n"
        "[click](http://evil.example) and `code` and <b>bold</b>\n"
        "| col | col |"
    )

    @pytest.fixture
    def hostile_env(self, demo_env, tmp_path):
        path = tmp_path / "hostile.duckdb"
        reviews = [
            _review(1, text=self.EVIL),
            _review(2, text=self.EVIL),  # identical pair -> repeated-text family
            _review(3, text="A genuinely different organic review about pastries."),
            _review(4, text="Another organic review mentioning the terrace."),
            _review(5, text="Friendly staff, quick service, fair prices."),
        ]
        _build_demo_artifact(path, reviews)
        os.environ[ENV_DB_PATH] = str(path)
        yield path
        os.environ.pop(ENV_DB_PATH, None)

    def test_reviewers_page_escapes_review_text(self, hostile_env) -> None:
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        at.sidebar.radio[0].set_value("Reviewers")
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]
        # Open the profile that owns the hostile review.
        at.selectbox[0].set_value("rv-001")
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]
        rendered = _rendered(at)
        assert "EVIL\\_HEADING" in rendered
        for line in rendered.splitlines():
            assert not line.lstrip().startswith("# EVIL")
        assert "\\# EVIL" in rendered
        assert "[click](http://evil.example)" not in rendered
        assert "\\[click\\]" in rendered
        assert "<b>bold</b>" not in rendered

    def test_duplicates_page_escapes_review_text(self, hostile_env) -> None:
        at = AppTest.from_file(APP_PATH, default_timeout=180)
        at.run()
        at.sidebar.radio[0].set_value("Duplicates")
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]
        rendered = _rendered(at)
        assert "EVIL\\_HEADING" in rendered
        assert "\\# EVIL" in rendered
        assert "[click](http://evil.example)" not in rendered
        # Reviewer identifiers are plain text too — no code-span breakout.
        assert "`rv-001`" not in rendered


# ---------------------------------------------------------------------------
# Annotation tooling must stay local
# ---------------------------------------------------------------------------


class TestLabelingGuard:
    def test_labeling_app_refuses_public_demo(self, demo_env) -> None:
        at = AppTest.from_file(APP_LABELING_PATH, default_timeout=120)
        at.run()
        errors = [e.value for e in at.error]
        assert errors, "labeling app did not report the demo-mode refusal"
        assert "disabled in public-demo mode" in errors[0]
        # No annotation widgets are instantiated before the stop.
        assert not list(at.radio)

    def test_labeling_app_runs_locally_without_the_flag(self, monkeypatch, tmp_path) -> None:
        monkeypatch.delenv(ENV_FLAG, raising=False)
        monkeypatch.setenv("RS_VALIDATION_DIR", str(tmp_path / "no_such_dir"))
        at = AppTest.from_file(APP_LABELING_PATH, default_timeout=120)
        at.run()
        assert not any("disabled in public-demo mode" in e.value for e in at.error)
