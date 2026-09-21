"""Overview page (SPEC.md §27): score cards, timelines, explainability.

Product hierarchy: place + headline numbers first, then the raw-vs-weighted
rating verdict, then charts, with technical metrics behind an expander.
"""

from __future__ import annotations

import pandas as pd

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.storage import DuckDBStore

from .common import (
    FilterState,
    confidence_badge,
    filter_reviews,
    friendly_counter_signal,
    friendly_signal,
    grouped_evidence,
    info_state,
    technical_details,
)


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


def _place_meta(store: DuckDBStore, place_id: str) -> dict:
    frame = store.list_places()
    rows = frame[frame["place_id"] == place_id]
    if rows.empty:
        return {"place_name": place_id, "place_category": None}
    return rows.iloc[0].to_dict()


def _weighted_verdict(raw: float, weighted: float) -> str:
    delta = raw - weighted
    if delta > 0.005:
        return f"Weighted rating is {delta:.2f} lower than raw rating ({raw:.2f})."
    if delta < -0.005:
        return f"Weighted rating is {abs(delta):.2f} higher than raw rating ({raw:.2f})."
    return "Weighted rating is essentially the same as the raw rating."


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
    meta = _place_meta(store, place_id)

    place_name = meta.get("place_name") or place_id
    place_category = meta.get("place_category") or "—"
    st.header(place_name)
    st.caption(f"Category: {place_category}")

    if not p.reviews:
        info_state(
            "No reviews for this place",
            "There are no review records for this place in the dataset.",
            hint="Pick another place from the sidebar to explore its reviews.",
        )
        return

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

    # --- headline numbers ---------------------------------------------------
    col1, col2, col3 = st.columns(3)
    col1.metric("Raw rating", f"{p.raw_rating:.2f}")
    col2.metric("Weighted rating", f"{p.weighted_rating_value:.2f}")
    col3.metric("Reviews", p.review_count)

    suspicion = (p.coordinated.confidence if p.coordinated else None) or "LOW"
    col4, col5, col6 = st.columns(3)
    col4.metric("Reviewers", len(reviewers))
    col5.metric("Suspicious activity", confidence_badge(suspicion))
    col6.metric("Duplicate rate", f"{duplicate_rate:.1f}%")

    if flt.active():
        st.caption(f"Charts below show {len(reviews)} of {p.review_count} reviews (sidebar filters active).")

    st.markdown("---")

    # --- rating verdict -----------------------------------------------------
    st.markdown("### Rating overview")
    st.markdown(_weighted_verdict(p.raw_rating, p.weighted_rating_value))

    contrib_signals, contrib_counters = grouped_evidence(p.weighted_rating_result)
    coord_signals, coord_counters = grouped_evidence(p.coordinated)

    if contrib_signals or coord_signals:
        st.markdown("**Main contributing signals**")
        seen: set[str] = set()
        for s in contrib_signals + coord_signals:
            if s in seen:
                continue
            seen.add(s)
            st.markdown(f"- {s}")
    counters = [c for c in contrib_counters + coord_counters if c]
    if counters:
        st.markdown("**Counter-signals**")
        for c in counters[:4]:
            st.markdown(f"- {c}")

    # --- coordinated activity ----------------------------------------------
    coord = p.coordinated
    if coord is not None:
        st.markdown(
            f"**Coordinated activity** — score {coord.rendered_value()} · "
            f"{confidence_badge(coord.confidence)}"
        )
        if coord.signals:
            st.markdown("**Why:**")
            for s in coord.signals:
                st.markdown(f"- :green[+ {friendly_signal(s)}]")
        if coord.counter_signals:
            st.markdown("**Counter-signals:**")
            for s in coord.counter_signals:
                st.markdown(f"- :red[− {friendly_counter_signal(s)}]")
        if coord.details:
            technical_details(
                [(f"Component: {k}", f"{v:.3f}") for k, v in sorted(coord.details.items())],
                key="coordinated",
            )

    # --- charts --------------------------------------------------------------
    st.markdown("---")
    st.markdown("### How ratings change over time")

    col_a, col_b, col_c = st.columns(3)
    with col_a:
        st.markdown("**Rating distribution**")
        dist = _rating_distribution(reviews)
        if dist["count"].sum() == 0:
            st.caption("No reviews with ratings in the current selection.")
        else:
            fig = px.bar(
                dist,
                x="rating",
                y="count",
                labels={"rating": "Star rating", "count": "Number of reviews"},
                text="count",
            )
            fig.update_layout(showlegend=False, height=260, margin=dict(l=0, r=0, t=10, b=0))
            fig.update_traces(textposition="outside")
            st.plotly_chart(fig, width="stretch")
    with col_b:
        st.markdown("**Reviews over time**")
        over_time = _reviews_over_time(reviews)
        if over_time.empty:
            st.caption("No review dates in the current selection.")
        else:
            fig2 = px.histogram(
                over_time,
                x="date",
                nbins=40,
                labels={"date": "Date", "count": "Number of reviews"},
            )
            fig2.update_layout(showlegend=False, height=260, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig2, width="stretch")
    with col_c:
        st.markdown("**Daily mean rating**")
        rating_time = _rating_over_time(reviews)
        if rating_time.empty:
            st.caption("No rated reviews with dates in the current selection.")
        else:
            fig3 = px.line(
                rating_time,
                x="date",
                y="mean_rating",
                labels={"date": "Date", "mean_rating": "Mean rating"},
                custom_data=["count"],
            )
            fig3.update_traces(hovertemplate="%{x}<br>mean rating %{y:.2f} · %{customdata[0]} reviews<extra></extra>")
            fig3.update_layout(height=260, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig3, width="stretch")

    # --- technical scores -----------------------------------------------------
    if p.weighted_rating_result is not None:
        wr = p.weighted_rating_result
        technical_details(
            [
                ("Raw rating", f"{wr.details.get('raw', p.raw_rating):.3f}"),
                ("Weighted rating", f"{wr.value:.3f}"),
                ("Delta (raw − weighted)", f"{wr.details.get('delta', 0.0):.3f}"),
                ("Confidence", wr.confidence.value),
                ("Raw signals", " · ".join(wr.signals) or "—"),
                ("Raw counter-signals", " · ".join(wr.counter_signals) or "—"),
            ],
            key="weighted_rating",
        )

    if p.keywords:
        st.markdown("---")
        st.markdown("### What people mention most")
        st.caption("Frequent words and short phrases across the selected reviews.")
        cols = st.columns(min(5, len(p.keywords)))
        for i, (kw, _score) in enumerate(p.keywords[: min(5, len(p.keywords))]):
            cols[i].markdown(f"**{kw}**")
