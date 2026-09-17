"""Overview page (SPEC.md §27): score cards, timelines, explainability."""

from __future__ import annotations

import pandas as pd

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import FilterState, explain, filter_reviews


def _rating_distribution(reviews: list) -> pd.DataFrame:
    counts = pd.Series([r.rating for r in reviews if r.rating is not None]).value_counts()
    return counts.reindex([1, 2, 3, 4, 5], fill_value=0).rename_axis("rating").reset_index(name="count")


def _reviews_over_time(reviews: list) -> pd.DataFrame:
    dates = pd.to_datetime([r.published_at[:10] for r in reviews if r.published_at])
    return dates.to_frame(name="date")


def _rating_over_time(reviews: list) -> pd.DataFrame:
    rows = [
        {"date": r.published_at[:10], "rating": r.rating}
        for r in reviews
        if r.published_at and r.rating is not None
    ]
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.groupby("date").agg(mean_rating=("rating", "mean"), count=("rating", "size")).reset_index()


def render_overview_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    import plotly.express as px
    import streamlit as st

    p = engine.analyze(place_id)
    reviews = filter_reviews(p.reviews, flt)

    st.header("Overview")

    reviewers = {r.reviewer_id for r in p.reviews}
    duplicate_rate = 0.0
    dup_ids = {
        rid
        for g in p.duplicate_groups
        if len(g.review_ids) >= 3
        for rid in g.review_ids
    }
    if p.reviews:
        duplicate_rate = round(len(dup_ids) / len(p.reviews) * 100, 1)

    col1, col2, col3 = st.columns(3)
    col1.metric("Raw rating", f"{p.raw_rating:.2f}")
    col2.metric("Weighted rating", f"{p.weighted_rating_value:.2f}")
    col3.metric("Reviews", p.review_count)

    col4, col5, col6 = st.columns(3)
    col4.metric("Reviewers", len(reviewers))
    suspicion = p.coordinated.confidence.value if p.coordinated else "LOW"
    col5.metric("Suspicious activity", suspicion)
    col6.metric("Duplicate rate", f"{duplicate_rate:.1f}%")

    st.markdown("---")

    left, right = st.columns(2)
    with left:
        st.subheader("Ratings distribution")
        dist = _rating_distribution(reviews)
        fig = px.bar(dist, x="rating", y="count", labels={"rating": "Rating", "count": "Reviews"})
        st.plotly_chart(fig, width="stretch")

        st.subheader("Reviews over time")
        over_time = _reviews_over_time(reviews)
        if not over_time.empty:
            fig2 = px.histogram(over_time, x="date", nbins=40, labels={"date": "Date", "count": "Reviews"})
            st.plotly_chart(fig2, width="stretch")

    with right:
        st.subheader("Rating over time")
        rating_time = _rating_over_time(reviews)
        if not rating_time.empty:
            fig3 = px.line(
                rating_time,
                x="date",
                y="mean_rating",
                labels={"date": "Date", "mean_rating": "Mean rating"},
            )
            st.plotly_chart(fig3, width="stretch")

    st.markdown("---")

    st.subheader("Explanation")
    if p.weighted_rating_result is not None:
        explain(p.weighted_rating_result, title="Weighted rating vs raw")
    if p.coordinated is not None:
        explain(p.coordinated, title="Coordinated activity")
