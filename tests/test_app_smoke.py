"""Streamlit AppTest smoke test (SPEC.md §39-7..§39-20).

Runs the real ``app.py`` headless via Streamlit's AppTest harness and visits
every page. The place cookie is the demo place p5 (negative bombing) to
exercise the HIGH-coordinated and weighted-rating paths.

Browser is unavailable in this environment, so this is the HTTP/process-level
smoke substitute required by SPEC.md §39 ("if browser unavailable, use
HTTP/process smoke test and record the limitation").
"""

from __future__ import annotations

from pathlib import Path

import pytest

APP_PATH = str(Path(__file__).resolve().parents[1] / "app.py")

streamlit = pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402


def _app() -> AppTest:
    return AppTest.from_file(APP_PATH, default_timeout=180)


class TestAppSmoke:
    def test_app_boots_without_exception(self) -> None:
        at = _app()
        at.run()
        assert not list(at.exception), [e.value for e in at.exception]

    def test_all_pages_run(self) -> None:
        at = _app()
        at.run()
        assert not list(at.exception)
        radio = at.sidebar.radio[0]
        for page in [
            "Overview",
            "Topics",
            "Anomalies",
            "Duplicates",
            "Reviewers",
            "Reviewed Places",
            "Data Quality",
        ]:
            radio.set_value(page)
            at.run()
            assert not list(at.exception), (page, [e.value for e in at.exception])

    def test_overview_shows_rating_metrics(self) -> None:
        at = _app()
        at.run()
        at.sidebar.radio[0].set_value("Overview")
        at.run()
        metric_values = {m.label: m.value for m in at.metric}
        assert "Raw rating" in metric_values
        assert "Weighted rating" in metric_values
        assert "Reviews" in metric_values

    def test_explainability_rendered(self) -> None:
        at = _app()
        at.run()
        at.sidebar.radio[0].set_value("Overview")
        at.run()
        md = "\n".join(m.value for m in at.markdown)
        assert "Coordinated activity" in md
        assert "Counter-signals" in md or "+ " in md
