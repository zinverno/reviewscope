"""ReviewScope — Streamlit app (SPEC.md §26).

Sidebar: dataset, place, review filters, page.  Pages: Overview, Topics,
Anomalies, Duplicates, Reviewers, Reviewed Places, Data Quality.

Run: ``streamlit run app.py``
"""

from __future__ import annotations

import streamlit as st

from reviewscope.ui import (
    render_anomalies_page,
    render_data_quality_page,
    render_duplicates_page,
    render_overview_page,
    render_reviewed_places_page,
    render_reviewers_page,
    render_topics_page,
)
from reviewscope.ui.common import DEFAULT_DB_PATH, FilterState, db_exists, get_engine, get_store

st.set_page_config(page_title="ReviewScope", layout="wide")

_PAGE_ORDER = ["Overview", "Topics", "Anomalies", "Duplicates", "Reviewers", "Reviewed Places", "Data Quality"]


def _sidebar() -> tuple[str, str, str, FilterState]:
    with st.sidebar:
        st.title("ReviewScope")
        st.caption("Dataset → place → filters → page")
        db_path = st.text_input("Dataset (duckdb path)", value=DEFAULT_DB_PATH)
        if not db_exists(db_path):
            st.error(f"Database not found at {db_path!r}. Generate the demo dataset first.")
            st.stop()

        store = get_store(db_path)
        places = store.list_places()
        if places.empty:
            st.error("No places in the dataset.")
            st.stop()

        place_options = places["place_id"].tolist()
        place_labels = [
            f"{row['place_name']} ({row['place_id']}) — {row['place_category']}, {row['review_count']} reviews"
            for _, row in places.iterrows()
        ]
        choice = st.selectbox("Place", place_options, format_func=lambda pid: place_labels[place_options.index(pid)])
        place_id = str(choice)

        flt = FilterState()
        with st.expander("Review filters", expanded=False):
            date_from = st.date_input("From", value=None)
            date_to = st.date_input("To", value=None)
            ratings = st.multiselect("Rating", [1, 2, 3, 4, 5])
            categories = st.multiselect("Category", sorted(places["place_category"].dropna().unique().tolist() or []))
            flt = FilterState(
                date_from=date_from.isoformat() if date_from else None,
                date_to=date_to.isoformat() if date_to else None,
                ratings=tuple(int(r) for r in ratings),
                categories=tuple(categories),
            )

        reviewers_all = st.toggle(
            "Restrict to specific reviewers",
            value=False,
            help="Turn this on to filter the dashboard to one or more reviewer accounts.",
        )
        if reviewers_all:
            engine = get_engine(db_path)
            p = engine.analyze(place_id)
            reviewer_choices = sorted({r.reviewer_id for r in p.reviews})
            chosen = st.multiselect("Reviewers", reviewer_choices)
            flt = FilterState(
                date_from=flt.date_from,
                date_to=flt.date_to,
                ratings=flt.ratings,
                categories=flt.categories,
                reviewers=tuple(chosen),
            )

        st.divider()
        page = st.radio(
            "Page",
            _PAGE_ORDER,
            help="Each page focuses on a different aspect of the review data.",
        )
    return db_path, place_id, page, flt


def main() -> None:
    db_path, place_id, page, flt = _sidebar()
    store = get_store(db_path)
    engine = get_engine(db_path)

    dispatch = {
        "Overview": render_overview_page,
        "Topics": render_topics_page,
        "Anomalies": render_anomalies_page,
        "Duplicates": render_duplicates_page,
        "Reviewers": render_reviewers_page,
        "Reviewed Places": render_reviewed_places_page,
        "Data Quality": render_data_quality_page,
    }
    handler = dispatch.get(page)
    if handler:
        handler(store, engine, place_id, flt)

    st.divider()
    st.caption(
        "This app shows the analysis output — it does not assert fraud, "
        "intent, or verified physical movement."
    )


if __name__ == "__main__":
    main()
