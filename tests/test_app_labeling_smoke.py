"""Phase 15 — AppTest smoke for the blind labeling app.

The labeling app must be **blind by construction**: it never loads scores, so
the smoke test asserts the static guarantee (no score fields in its imports of
the selection/annotation artifacts) by provisioning a toy validation directory
and verifying the app boots, renders a review, and persists a verdict without
any reference to detector output.

Browser is unavailable in this environment; AppTest is the HTTP/process-level
smoke substitute (SPEC.md §39).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reviewscope.models.review import NormalizedReview
from reviewscope.storage import DuckDBStore

APP_LABELING = str(Path(__file__).resolve().parents[1] / "app_labeling.py")

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


def _provision_validation_dir(tmp_path: Path) -> Path:
    reviews = [
        NormalizedReview(
            review_id="sel-1",
            place_id="pX",
            place_name="Тестовое место",
            place_category="coffee",
            reviewer_id="u1",
            rating=5,
            text="Очень вкусный раф, обслуживание быстрое, понравился десерт.",
            published_at="2026-09-01",
        ),
        NormalizedReview(
            review_id="sel-2",
            place_id="pX",
            place_name="Тестовое место",
            place_category="coffee",
            reviewer_id="u2",
            rating=4,
            text="Латте хороший, интерьер приятный.",
            published_at="2026-09-02",
        ),
        NormalizedReview(
            review_id="not-selected",
            place_id="pX",
            place_name="Тестовое место",
            place_category="coffee",
            reviewer_id="u3",
            rating=5,
            text="Вне очереди, не должен показываться.",
            published_at="2026-09-03",
        ),
    ]
    with DuckDBStore(tmp_path / "dataset.duckdb") as store:
        store.ingest(reviews)

    selection = {
        "entries": [
            {"review_id": "sel-1", "sample_type": "evaluation", "sampling_stratum": "random",
             "place_id": "pX", "in_predicted_duplicate_group": False},
            {"review_id": "sel-2", "sample_type": "evaluation", "sampling_stratum": "random",
             "place_id": "pX", "in_predicted_duplicate_group": False},
        ]
    }
    (tmp_path / "sample_selection.json").write_text(
        json.dumps(selection), encoding="utf-8"
    )
    return tmp_path


def _app() -> AppTest:
    return AppTest.from_file(APP_LABELING, default_timeout=180)


def test_labeling_app_boots_blind_and_saves(tmp_path, monkeypatch):
    vdir = _provision_validation_dir(tmp_path)
    monkeypatch.setenv("RS_VALIDATION_DIR", str(vdir))
    monkeypatch.setenv("RS_ANNOTATOR_ID", "smoke-tester")

    at = _app()
    at.run()
    assert not list(at.exception), [e.value for e in at.exception]

    assert at.subheader, "app must render the current review id in a subheader"
    assert "sel-1" in at.subheader[0].value
    rendered = "\n".join(m.value for m in at.markdown)
    assert "Очень вкусный раф" in rendered
    assert "Progress" in rendered
    # Blindness: no ReviewScope score may ever be rendered.
    assert "templated_score" not in rendered
    assert "specificity_score" not in rendered
    assert "score_table" not in rendered

    # Save a verdict (default radio values) and advance.
    save_buttons = [b for b in at.button if b.label == "Save & Next"]
    assert save_buttons, at.button
    save_buttons[0].click()
    at.run()
    assert not list(at.exception), [e.value for e in at.exception]
    assert at.subheader, "after save, the next review should be shown"
    assert "sel-2" in at.subheader[0].value

    # The verdict must have been persisted; reopening the store sees it.
    from reviewscope.validation.annotation import AnnotationStore

    with AnnotationStore(vdir / "annotations.duckdb") as ann:
        assert ann.labeled_ids() == ["sel-1"]
        saved = ann.get_label("sel-1")
        assert saved.annotator_id == "smoke-tester"
        assert saved.templated_label is not None


def test_labeling_app_rejects_missing_selection(tmp_path, monkeypatch):
    vdir = tmp_path  # no files provisioned
    monkeypatch.setenv("RS_VALIDATION_DIR", str(vdir))
    monkeypatch.setenv("RS_ANNOTATOR_ID", "smoke-tester")

    at = _app()
    at.run()
    # A friendly error is shown, not a raw exception.
    assert not list(at.exception), [e.value for e in at.exception]
    assert at.error, [e.value for e in at.error]
    assert "Selection file not found" in at.error[0].value
