"""Streamlit UI for ReviewScope (SPEC.md §26–§31, §36).

Pages render analysis results from :class:`~reviewscope.analysis.engine` and
always show score, confidence, signals and counter-signals — explainability is
a hard requirement (§36), a bare number is never enough.
"""

from reviewscope.ui.anomalies import render_anomalies_page
from reviewscope.ui.common import FilterState, get_engine, get_store
from reviewscope.ui.data_quality import render_data_quality_page
from reviewscope.ui.duplicates import render_duplicates_page
from reviewscope.ui.overview import render_overview_page
from reviewscope.ui.reviewed_places import render_reviewed_places_page
from reviewscope.ui.reviewers import render_reviewers_page
from reviewscope.ui.topics import render_topics_page

__all__ = [
    "FilterState",
    "get_engine",
    "get_store",
    "render_anomalies_page",
    "render_data_quality_page",
    "render_duplicates_page",
    "render_overview_page",
    "render_reviewed_places_page",
    "render_reviewers_page",
    "render_topics_page",
]
