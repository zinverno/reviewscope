"""Discover page (Phase 16): dataset-level cross-place analytics.

Discover answers the question a place-level dashboard cannot: *which
organizations in this dataset are worth investigating?* It aggregates the
analysis ReviewScope already produces for a single place across every place of
the selected dataset — no new detector, score or threshold is introduced here
(all numbers come from :mod:`reviewscope.discovery`).

Two product rules are enforced on this page:

1. **No verdicts.** Every section is a descriptive ranking of observed
   evidence. Nothing is called fake, fraudulent or manipulated.
2. **No invented evidence.** Where the dataset cannot support a metric the page
   renders ``N/A`` plus a capability note. A missing capability is never
   rendered as a zero, and never ranked as if it had been observed.
"""

from __future__ import annotations

import time

import pandas as pd
import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.discovery import (
    NOT_AVAILABLE,
    SORT_OPTIONS,
    DatasetSummary,
    display_category_frame,
    display_places_frame,
    filter_places,
    get_dataset_summary,
    ranking_sections,
    source_badge,
)
from reviewscope.storage import DuckDBStore

from .common import PLACE_KEY, FilterState, info_state, open_place

FOCUS_KEY = "discover_focus_place"
CATEGORY_FILTER_KEY = "discover_category_filter"
QUERY_KEY = "discover_query"
MIN_REVIEWS_KEY = "discover_min_reviews"
DUP_RANGE_KEY = "discover_dup_rate_range"
SORT_KEY = "discover_sort_by"
ORDER_KEY = "discover_sort_order"

_DISCOVERY_INTRO = (
    "Where the review evidence sits across every place in this dataset. "
    "Rankings are descriptive: a high position means a measured pattern, "
    "not a verdict about the organization."
)


def _load_summary(store: DuckDBStore, engine: AnalysisEngine) -> tuple[DatasetSummary, float]:
    """Fetch (or build once) the cached dataset summary, with a progress bar."""
    started = time.perf_counter()
    progress = st.progress(0.0, text="Analysing every place in this dataset (once per dataset)…")
    try:
        summary = get_dataset_summary(
            store.db_path or "",
            store,
            engine,
            progress=lambda done, total: progress.progress(min(1.0, done / max(total, 1))),
        )
    finally:
        progress.empty()
    return summary, time.perf_counter() - started


def _dataset_metrics(summary: DatasetSummary) -> None:
    caps = summary.capabilities
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Reviews", caps.total_reviews)
    c2.metric("Places", caps.total_places)
    c3.metric("Categories", caps.total_categories)
    c4.metric("Places with duplicate groups", summary.places_with_duplicate_groups)
    c5.metric("Places with topic clusters", summary.places_with_topic_clusters)


def _capability_notes(summary: DatasetSummary) -> None:
    """State what this dataset cannot show — before showing what it can."""
    for note in summary.capabilities.notes():
        st.info(note, icon="ℹ️")
    if summary.places_failed:
        st.warning(
            f"{summary.places_failed} place(s) could not be analysed and are listed with "
            f"{NOT_AVAILABLE} evidence. Open Data Quality to inspect the dataset."
        )


def _filter_controls(
    places: pd.DataFrame,
) -> tuple[tuple[str, ...], int, str, tuple[float, float], str, bool]:
    categories = sorted({str(c) for c in places["place_category"].dropna() if str(c).strip()})
    counts = places["review_count"].dropna()
    max_reviews = int(counts.max()) if len(counts) else 0
    with st.expander("Dataset filters", expanded=True):
        c1, c2, c3 = st.columns(3)
        c4, c5, c6 = st.columns(3)
        selected_categories = tuple(
            c1.multiselect("Category", categories, key=CATEGORY_FILTER_KEY, placeholder="All categories")
        )
        min_reviews = int(
            c2.number_input(
                "Minimum reviews",
                min_value=0,
                max_value=max(max_reviews, 0),
                value=0,
                step=1,
                key=MIN_REVIEWS_KEY,
                help="Hide places with fewer reviews than this.",
            )
        )
        query = str(c3.text_input("Place name contains", key=QUERY_KEY, placeholder="e.g. zoo"))
        dup_low, dup_high = c4.slider(
            "Duplicate rate range (%)",
            min_value=0.0,
            max_value=100.0,
            value=(0.0, 100.0),
            step=5.0,
            key=DUP_RANGE_KEY,
            help=(
                "Only places whose measured duplicate rate falls inside the range. "
                f"Places with no measured rate stay listed as {NOT_AVAILABLE}."
            ),
        )
        sort_by = str(c5.selectbox("Sort by", list(SORT_OPTIONS), key=SORT_KEY))
        descending = bool(c6.toggle("Highest first", value=True, key=ORDER_KEY))
    return selected_categories, min_reviews, query, (float(dup_low), float(dup_high)), sort_by, descending


def _focus_controls(filtered: pd.DataFrame) -> None:
    """Drill-down: turn a discovered place into the selected place."""
    options = filtered["place_id"].tolist()
    if not options:
        return
    labels = {
        str(row["place_id"]): (
            f"{row['place_name']} — {row['place_category'] or 'no category'}, "
            f"{int(row['review_count'])} reviews"
        )
        for _, row in filtered.iterrows()
    }
    current = str(st.session_state.get(PLACE_KEY, ""))
    preferred = current if current in options else options[0]
    if st.session_state.get(FOCUS_KEY) not in options:
        st.session_state[FOCUS_KEY] = preferred
    focus = str(
        st.selectbox(
            "Place to open",
            options,
            key=FOCUS_KEY,
            format_func=lambda pid: labels.get(str(pid), str(pid)),
        )
    )
    row = filtered[filtered["place_id"] == focus].iloc[0]
    st.caption(
        f"**{row['place_name']}** · {row['place_category'] or 'no category'} · "
        f"{int(row['review_count'])} reviews · raw rating "
        f"{_fmt(row['raw_rating'])} · weighted rating {_fmt(row['weighted_rating'])}"
    )
    b1, b2, b3 = st.columns(3)
    if b1.button("Open Overview", key="discover_open_overview", width="stretch"):
        open_place(focus, "Overview")
    if b2.button("Open Duplicates", key="discover_open_duplicates", width="stretch"):
        open_place(focus, "Duplicates")
    if b3.button("Open Topics", key="discover_open_topics", width="stretch"):
        open_place(focus, "Topics")
    st.caption(
        "Opening a place sets the sidebar Place selector and switches page. "
        "The selection persists while you move between pages."
    )


def _fmt(value: object) -> str:
    """Format a metric for the drill-down caption, ``N/A`` when unavailable."""
    if value is None or pd.isna(value):
        return NOT_AVAILABLE
    return f"{float(value):.2f}"


def _render_rankings(summary: DatasetSummary, filtered: pd.DataFrame) -> None:
    st.markdown("---")
    st.markdown("### Discovery views")
    st.caption(
        "Descriptive rankings of the places currently passing the filters. "
        "Position is a measurement, not an accusation: the same place can head "
        "a 'most 5★ reviews' list and a 'lowest specificity' list."
    )
    sections = ranking_sections(
        filtered,
        coordinated_available=summary.capabilities.coordinated_available,
    )
    left, right = st.columns(2)
    for index, section in enumerate(sections):
        column = left if index % 2 == 0 else right
        with column, st.container(border=True):
            st.markdown(f"**{section.title}**")
            st.caption(section.description)
            if section.available and not section.frame.empty:
                st.dataframe(
                    section.frame,
                    width="stretch",
                    hide_index=True,
                    key=f"discover_rank_{section.key}",
                )
            else:
                st.caption(section.note or "No data available for this ranking.")


def _render_categories(summary: DatasetSummary) -> None:
    st.markdown("---")
    st.markdown("### Category comparison")
    st.caption(
        "Medians and quartiles per category across all places in the dataset. "
        "Robust statistics are used so one large place cannot dominate a category. "
        "No category score is computed — this is context for the place rankings."
    )
    frame = display_category_frame(summary.categories)
    if frame.empty:
        st.info("No categories in this dataset.")
        return
    st.dataframe(frame, width="stretch", hide_index=True, key="discover_categories")


def _methodology(summary: DatasetSummary) -> None:
    caps = summary.capabilities
    st.markdown("---")
    st.markdown("#### How these numbers are produced")
    st.markdown(
        "- Every place is analysed with the production engine, then aggregated. "
        "Discover introduces no new detector, weight or threshold."
    )
    st.markdown(
        "- **Duplicate rate** = share of a place's reviews that sit in a "
        "repeated-text group of 3+ reviews — the same definition Overview uses. "
        "**Dup groups (2+)** counts every detected group, pairs included, like "
        "the Duplicates page, so the two columns can legitimately differ."
    )
    st.markdown(
        "- **Raw − weighted** = the raw average rating minus the weighted "
        "rating, both shown in the same row. It describes the weighting, not a "
        "correction claim. The unrounded production delta is on the place's "
        "Overview, under technical details."
    )
    st.markdown(
        "- **Specificity** is the production heuristic specificity score, "
        "summarised per place as median and mean."
    )
    if not caps.coordinated_available:
        st.markdown(
            "- **Coordinated activity** is not ranked here: its strongest "
            "components are temporal and this dataset has no publication "
            "timestamps. It remains available per place on Overview."
        )
    if not caps.ratings_available:
        st.markdown("- Rating columns are `N/A`: this dataset carries no ratings.")
    st.caption(
        "This page ranks observed evidence. It does not assert fraud, intent, "
        "coordinated behaviour, or verified physical movement."
    )


def render_discover_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    st.header("Discover")
    st.caption(_DISCOVERY_INTRO)

    if store.review_count() == 0:
        info_state(
            "No reviews in this dataset",
            "The selected dataset has no review records, so there is nothing to "
            "compare across places.",
            hint="Load a dataset in the sidebar, or generate the demo dataset.",
        )
        return

    summary, load_seconds = _load_summary(store, engine)

    _dataset_metrics(summary)
    _capability_notes(summary)
    st.caption(
        f"Dataset summary: {source_badge(summary.source)} · "
        f"cold build {summary.build_seconds:.1f}s · this rerun {load_seconds:.2f}s · "
        f"{summary.places_analyzed} places analysed · "
        f"embeddings reused from the persisted cache"
    )

    st.markdown("---")
    st.markdown("### Place comparison")
    places = summary.places
    categories, min_reviews, query, dup_range, sort_by, descending = _filter_controls(places)
    filtered = filter_places(
        places,
        categories=categories,
        min_reviews=min_reviews,
        query=query,
        duplicate_rate_range=dup_range,
        sort_by=sort_by,
        descending=descending,
    )
    st.caption(
        f"{len(filtered)} of {len(places)} places shown · sorted by "
        f"{sort_by} ({'highest' if descending else 'lowest'} first) · "
        f"`{NOT_AVAILABLE}` marks evidence this dataset cannot provide."
    )
    if filtered.empty:
        info_state(
            "No places match the current filters",
            "The dataset summary above is unchanged; only the comparison table "
            "below follows the filters.",
            hint=(
                "Loosen the filters (category, minimum reviews, place name or "
                "duplicate-rate range) to see places again."
            ),
        )
        return

    st.dataframe(
        display_places_frame(filtered),
        width="stretch",
        hide_index=True,
        key="discover_places_table",
    )

    _focus_controls(filtered)
    _render_rankings(summary, filtered)
    _render_categories(summary)
    _methodology(summary)
