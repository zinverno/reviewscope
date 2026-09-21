"""Reviewers page (SPEC.md §24): reviewer leaderboard, profile, and evidence.

A user-facing summary table comes first, then a per-reviewer profile built
from the existing category-experience / local-familiarity / relevance scores,
with technical aggregate metrics behind an expander.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from reviewscope.analysis.engine import AnalysisEngine
from reviewscope.analysis.reviewer import (
    category_experience_score,
    local_familiarity_score,
    reviewer_relevance_score,
)
from reviewscope.storage import DuckDBStore

from .common import (
    NO_DATA,
    FilterState,
    confidence_badge,
    human_duration,
    info_state,
    metric_band,
    value_or_nodata,
)


def _place_meta(store: DuckDBStore, place_id: str) -> dict:
    frame = store.list_places()
    rows = frame[frame["place_id"] == place_id]
    if rows.empty:
        return {}
    return rows.iloc[0].to_dict()


def _leaderboard_frame(metrics: list, total_reviews: int) -> pd.DataFrame:
    ordered = sorted(metrics, key=lambda m: m.review_count, reverse=True)[:20]
    rows = [
        {
            "Reviewer": m.reviewer_id,
            "Reviews": m.review_count,
            "Share of reviews": pct(m.review_count / total_reviews),
            "Mean rating": f"{m.rating_mean:.2f}" if m.rating_mean else "—",
            "Activity": human_duration(m.active_period_days),
        }
        for m in ordered
    ]
    return pd.DataFrame(rows)


def _profile_reviews(place_reviews: list, reviewer_id: str, limit: int = 10) -> list:
    return [r for r in place_reviews if r.reviewer_id == reviewer_id][:limit]


def pct(frac: float) -> str:
    return f"{frac * 100:.0f}%"


def render_reviewers_page(
    store: DuckDBStore,
    engine: AnalysisEngine,
    place_id: str,
    flt: FilterState,
) -> None:
    p = engine.analyze(place_id)
    st.header("Reviewers")
    st.caption(
        "The review accounts behind this place's reviews, their activity, and "
        "what the evidence can say about their reviewing patterns."
    )

    if not p.reviews:
        info_state(
            "No reviewers",
            "There are no reviews for this place in the dataset.",
        )
        return

    metrics = p.reviewer_overview or []
    if not metrics:
        info_state(
            "No reviewer data",
            "The analysis did not produce per-reviewer aggregates for this place.",
        )
        return

    meta = _place_meta(store, place_id)
    category = meta.get("place_category")
    city = meta.get("city")
    region = meta.get("region")

    history_by_reviewer = engine._history_by_reviewer()
    by_metric = {m.reviewer_id: m for m in metrics}

    total_reviews = sum(m.review_count for m in metrics) or 1
    top_reviewer = max(metrics, key=lambda m: m.review_count, default=None)

    # --- headline summary ------------------------------------------------------
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Reviewers", len(metrics))
    col2.metric("Place reviews", p.review_count)
    col3.metric("Avg reviews / reviewer", f"{total_reviews / max(len(metrics), 1):.1f}")
    col4.metric(
        "Top reviewer share",
        f"{top_reviewer.review_count / total_reviews * 100:.0f}%" if top_reviewer else "—",
    )

    st.markdown("---")
    st.markdown("### Most active reviewers")
    st.caption("Counts cover this place's reviews. Reviewer identity in this dataset is the platform account.")
    st.dataframe(_leaderboard_frame(metrics, total_reviews), width="stretch")

    # --- per-reviewer profile ----------------------------------------------------
    st.markdown("---")
    options = sorted(metrics, key=lambda m: m.review_count, reverse=True)
    labels = {
        m.reviewer_id: f"{m.reviewer_id} — {m.review_count} reviews here"
        for m in options
    }
    selected = st.selectbox(
        "Reviewer profile",
        [m.reviewer_id for m in options],
        format_func=lambda rid: labels[rid],
    )
    m = by_metric.get(selected)
    if m is None:
        return
    history = history_by_reviewer.get(selected, [])

    with st.container(border=True):
        st.markdown(f"**{selected}**")
        st.caption(
            f"{m.review_count} reviews of this place"
            + (f" · {human_duration(m.active_period_days)} between first and last review" if m.active_period_days else "")
        )

        col_a, col_b, col_c = st.columns(3)
        col_a.metric("Place reviews", m.review_count)
        col_b.metric("Reviews overall (dataset)", len(history))
        col_c.metric("Mean rating here", f"{m.rating_mean:.2f}" if m.rating_mean else "—")

        if history:
            cats = {r.place_category or "unknown" for r in history}
            cities = {r.city or r.region or "unknown" for r in history}
            st.caption(
                f"Across the dataset: {len(cats)} categor{'y' if len(cats) == 1 else 'ies'} · "
                f"{len(cities)} location{'s' if len(cities) != 1 else ''}"
            )

        st.markdown("**Profile evidence**")
        profile = [
            ("Category experience", category_experience_score(history, category)),
            ("Local familiarity", local_familiarity_score(history, city, region)),
            ("Reviewer relevance", reviewer_relevance_score(history, category, city, region)),
        ]
        cols = st.columns(3)
        for col, (title, res) in zip(cols, profile, strict=True):
            with col:
                st.markdown(f"**{title}**")
                if value_or_nodata(res.value) == NO_DATA:
                    st.markdown(NO_DATA)
                else:
                    st.markdown(f"{confidence_badge(metric_band(res.value))} · {res.value:.0f}/100")
                signals = " · ".join(res.signals) or "—"
                counters = " · ".join(res.counter_signals) or "—"
                st.caption(f"Signals: {signals}")
                st.caption(f"Counter: {counters}")

        with st.expander("Technical metrics (aggregate)", expanded=False):
            frame = pd.DataFrame(
                [
                    {
                        "Rating entropy": m.rating_entropy if m.rating_entropy else None,
                        "Rating std": m.rating_std if m.rating_std else None,
                        "Review diversity": m.review_diversity if m.review_diversity else None,
                        "Review consistency": m.review_consistency if m.review_consistency else None,
                        "Duplicate ratio": m.duplicate_ratio,
                        "Template ratio": m.template_ratio,
                        "Mean specificity": m.specificity_mean if m.specificity_mean else None,
                        "Mean text length": m.text_length_mean if m.text_length_mean else None,
                    }
                ]
            )
            st.dataframe(frame, width="stretch")
            st.caption(
                "Aggregates over this place's reviews by this reviewer. Values of 0 can mean "
                "insufficient history rather than a zero signal."
            )

        st.markdown("**Reviews of this place by this reviewer:**")
        place_rows = _profile_reviews(p.reviews, selected)
        if not place_rows:
            st.caption("No reviews of this place by this reviewer on record.")
        for r in place_rows:
            rating = f"{r.rating}★" if r.rating is not None else "no rating"
            when = (r.published_at or "no date")[:10]
            st.markdown(f"- {rating} · {when} — {r.text_or_empty()[:140]}")
