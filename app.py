"""ReviewScope — Streamlit app (SPEC.md §26).

Sidebar: dataset, place, review filters, page.  Pages: Overview, Discover,
Topics, Anomalies, Duplicates, Reviewers, Reviewed Places, Data Quality.

Run: ``streamlit run app.py``

Public demo (``RS_PUBLIC_DEMO=1`` or the ``public_demo`` secret): the dataset
picker is replaced by the packaged synthetic dataset, the store is opened
read-only and no visitor action writes to disk. See docs/DEPLOYMENT.md.
"""

from __future__ import annotations

from functools import lru_cache

import streamlit as st

from reviewscope.public_demo import (
    DEMO_DATASET_LABEL,
    SYNTHETIC_DATA_BADGE,
    SYNTHETIC_DATA_NOTE,
    configure_public_demo,
    missing_embedding_keys,
    public_demo_db_path,
    public_demo_enabled,
)
from reviewscope.ui import (
    render_anomalies_page,
    render_data_quality_page,
    render_discover_page,
    render_duplicates_page,
    render_overview_page,
    render_reviewed_places_page,
    render_reviewers_page,
    render_topics_page,
)
from reviewscope.ui.common import (
    DEFAULT_DB_PATH,
    PAGE_KEY,
    PAGE_ORDER,
    PLACE_KEY,
    FilterState,
    apply_pending_navigation,
    db_exists,
    get_engine,
    get_store,
)

st.set_page_config(page_title="ReviewScope", layout="wide")

#: Public-demo mode is resolved once per process run (see docs/DEPLOYMENT.md).
PUBLIC_DEMO = public_demo_enabled()
if PUBLIC_DEMO:
    configure_public_demo()
    DEMO_DB_PATH = str(public_demo_db_path())
else:
    DEMO_DB_PATH = DEFAULT_DB_PATH

_PAGE_ORDER = list(PAGE_ORDER)


@lru_cache(maxsize=1)
def _demo_preflight(db_path: str) -> tuple[tuple[str, str, str], ...]:
    """Prove the packaged dataset carries every embedding before any page runs.

    The public instance intentionally ships without PyTorch: analysis reads
    precomputed vectors. An incomplete artifact must stop the app with a clear
    message rather than fail halfway through a page.
    """
    store = get_store(db_path)
    missing = missing_embedding_keys(store, store.fetch_reviews())
    return tuple(missing)


def _demo_dataset_error(db_path: str) -> None:
    st.error(
        f"Packaged demo dataset not found at `{db_path}`. "
        "Rebuild it with `python scripts/build_public_demo_dataset.py`."
    )
    st.stop()


def _sidebar() -> tuple[str, str, str, FilterState]:
    with st.sidebar:
        st.title("ReviewScope")
        if PUBLIC_DEMO:
            st.caption(SYNTHETIC_DATA_BADGE)
            st.caption("Dataset → place → filters → page")
            db_path = DEMO_DB_PATH
            if not db_exists(db_path):
                _demo_dataset_error(db_path)
            st.caption(f"Dataset: {DEMO_DATASET_LABEL}")
        else:
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
        # A page body may have queued a place/page switch (Discover drill-down):
        # apply it before the widgets are instantiated.
        apply_pending_navigation(place_options)
        if PLACE_KEY not in st.session_state or st.session_state[PLACE_KEY] not in place_options:
            st.session_state[PLACE_KEY] = place_options[0]
        choice = st.selectbox(
            "Place",
            place_options,
            key=PLACE_KEY,
            index=None,
            format_func=lambda pid: place_labels[place_options.index(pid)],
        )
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
            key=PAGE_KEY,
            index=0,
            help="Each page focuses on a different aspect of the review data.",
        )
    return db_path, place_id, page, flt


def _demo_preflight_guard() -> None:
    """Stop the public demo before rendering if the artifact cannot serve it."""
    if not db_exists(DEMO_DB_PATH):
        _demo_dataset_error(DEMO_DB_PATH)
    missing = _demo_preflight(DEMO_DB_PATH)
    if missing:
        st.error(
            f"The packaged demo dataset is missing {len(missing)} precomputed "
            "embeddings, and this deployment runs without the model. Rebuild "
            "with `python scripts/build_public_demo_dataset.py`."
        )
        st.stop()


def main() -> None:
    if PUBLIC_DEMO:
        _demo_preflight_guard()
    db_path, place_id, page, flt = _sidebar()
    store = get_store(db_path)
    engine = get_engine(db_path)

    if PUBLIC_DEMO:
        st.info(SYNTHETIC_DATA_NOTE)

    dispatch = {
        "Overview": render_overview_page,
        "Discover": render_discover_page,
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
    if PUBLIC_DEMO:
        st.caption(SYNTHETIC_DATA_BADGE)


if __name__ == "__main__":
    main()
