"""ReviewScope — Streamlit app (SPEC.md §26).

Sidebar: dataset, place, date range, rating, category. Pages: Overview,
Topics, Anomalies, Duplicates, Reviewers, Reviewed Places, Data Quality.

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


def _sidebar() -> tuple[str, str, str, FilterState]:
    with st.sidebar:
        st.title("ReviewScope")
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

        date_from = st.date_input("From", value=None)
        date_to = st.date_input("To", value=None)
        ratings = st.multiselect("Rating", [1, 2, 3, 4, 5])
        categories = st.multiselect("Category", sorted(places["place_category"].dropna().unique().tolist() or []))
        reviewers_all = st.toggle("Restrict to reviewers", value=False)

        flt = FilterState(
            date_from=date_from.isoformat() if date_from else None,
            date_to=date_to.isoformat() if date_to else None,
            ratings=tuple(int(r) for r in ratings),
            categories=tuple(categories),
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

        page = st.radio(
            "Page",
            ["Overview", "Topics", "Anomalies", "Duplicates", "Reviewers", "Reviewed Places", "Data Quality"],
        )
    return db_path, place_id, page, flt


def main() -> None:
    db_path, place_id, page, flt = _sidebar()
    store = get_store(db_path)
    engine = get_engine(db_path)

    if page == "Overview":
        render_overview_page(store, engine, place_id, flt)
    elif page == "Topics":
        render_topics_page(store, engine, place_id, flt)
    elif page == "Anomalies":
        render_anomalies_page(store, engine, place_id, flt)
    elif page == "Duplicates":
        render_duplicates_page(store, engine, place_id, flt)
    elif page == "Reviewers":
        render_reviewers_page(store, engine, place_id, flt)
    elif page == "Reviewed Places":
        render_reviewed_places_page(store, engine, place_id, flt)
    elif page == "Data Quality":
        render_data_quality_page(store, engine, place_id, flt)


if __name__ == "__main__":
    main()
